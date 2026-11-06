"""Node-owned pipeline observations over authenticated LAN/WebRTC RPC.

The coordinator contributes queue definitions and verified completion receipts.
Execution measurements come only from the owning node, never mirrored reports.
"""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import json
import logging
import re
import threading
import time
from agent_client import AgentClient, CONFIG
from node_pipeline_api import queue_snapshot, report_key
from task_pipeline import resources, runtime_observation, runtime_summary
from pipeline_workers import project as cache_workers

POOL=ThreadPoolExecutor(max_workers=5,thread_name_prefix='pipeline-nodes')
LOCK=threading.Lock()
CACHE={}
QUEUE=Path.home()/'.local/state/swarm-control/submission/queue.sqlite'


def collect(name,address,previous,task_ids,wan_only=False):
    client=None
    value={**previous,'host':name}
    stage='status'
    try:
        client=AgentClient({'host':name,'agent_endpoint':'http://'+address+':8790','agent_route':'webrtc' if wan_only else 'auto'})
        status=client.call('status',timeout=8)
        if status.get('node')!=name:raise ValueError('Node identity differs')
        stage='worker.records'
        records=dict(previous.get('records',{}));cursor=previous.get('cursor','')
        # Bound each polling turn. Larger journals warm up over multiple turns.
        for _ in range(10):
            page=client.call('worker.records',{'after':cursor},timeout=8)
            for row in page['items']:records[row['job']]=row
            cursor=page.get('next')
            if not cursor:break
        # Active attempts are refreshed independently of the history scan.
        for worker in status.get('workers',[])[:100]:
            if worker.get('group') in ('stream-work','shared-cache') or not re.fullmatch('[a-f0-9]{32}',str(worker.get('job',''))) or not worker.get('container'):continue
            try:
                row=client.call('worker.status',{'job':worker['job'],'container':worker['container']},timeout=8)
                if row.get('found'):records[row['job']]=row
                elif row.get('last_observation'):records[worker['job']]=row['last_observation']
            except (ValueError,RuntimeError):
                # A resource lease can belong to another node API (native test
                # jobs), or its container can disappear between these reads.
                # Neither makes the node itself unreachable.
                continue
        stage='stream.timings'
        streams=dict(previous.get('streams',{}))
        offset=previous.get('stream_offset',0)
        # Poll active jobs first, then sweep history so terminal receipts also
        # come from their owner. A bounded cache never labels history as live.
        active=[i for i,terminal in task_ids if not terminal]
        historical=[i for i,terminal in task_ids if terminal]
        chosen=list(dict.fromkeys(active+historical[offset:offset+300]))
        for start in range(0,len(chosen),100):
            streams.update(client.call('stream.timings',{'ids':chosen[start:start+100]},timeout=8)['items'])
        value={'host':name,'status':'live','observed_at':time.time(),'records':records,'cursor':cursor or '',
               'streams':streams,'stream_offset':offset+300 if offset+300<len(historical) else 0,
               'infrastructure':cache_workers(name,status,time.time())}
    except Exception as error:
        logging.warning('Pipeline node %s: %s unavailable (%s)',name,stage,type(error).__name__)
        value.update(status='unavailable',error='Node API unavailable: '+name)
    finally:
        if client is not None:client.close()
        with LOCK:CACHE[name].update(value=value,busy=False,checked=time.monotonic())


def execution_from_worker(row):
    fields=('container','image','sink','started','finished','elapsed_seconds','cpu_core_seconds',
            'cpu_throttled_seconds','cpu_sampled_at','cpu_measurement_basis','cpu_accounting_coverage',
            'memory_current_bytes','memory_peak_bytes','memory_swap_bytes','memory_sampled_at',
            'memory_measurement_basis','memory_accounting_coverage')
    value={k:row[k] for k in fields if k in row}
    value['started']=value.get('started') or row.get('started_at')
    value['finished']=value.get('finished') or row.get('finished_at')
    value.update(resources=resources(row),recorded_at=row.get('observed_at'),
                 phase='running' if row.get('running') else 'completed' if row.get('exit_code')==0 else 'failed',
                 execution_owner='jswarm-node')
    return value


def merge_item(item,node):
    item=dict(item);name=item.get('worker','').split('#')[0] if item.get('worker') else None
    item['data_sources']={'queue':item.get('queue_node'),'execution':name}
    if not name:return item
    item['node_observation']={k:node.get(k) for k in ('status','observed_at')}
    execution=None
    if item.get('task_id') and item['task_id'] in node.get('streams',{}):
        execution=dict(node['streams'][item['task_id']]);state=execution.get('state')
        execution.update(phase='running' if state=='running' else state,execution_owner='jswarm-node')
    else:
        if 'report_index' in node:
            matches=[node['report_index'][key] for key in item.get('report_keys',[]) if key in node['report_index']]
            if item.get('task_id') in node.get('records',{}):matches.append(node['records'][item['task_id']])
        else:
            matches=[r for r in node.get('records',{}).values() if
                     r.get('job')==item.get('task_id') or
                     r.get('controller_report') and report_key(r['controller_report']) in item.get('report_keys',[])]
        if matches:execution=execution_from_worker(max(matches,key=lambda r:r.get('observed_at',0)))
    if execution:
        item['execution']=execution
        item['runtime']=runtime_observation(execution,{},name)
        for k in ('cpu_accounting_coverage','queue_seconds','queue_timing_basis'):
            if k in execution:item['runtime'][k]=execution[k]
        item['worker_resources']=resources(execution.get('resources'))
        item['resources']=item['worker_resources'] or item['resources']
        item['peak_memory_bytes']=execution.get('memory_peak_bytes')
        # Queue completion means verified acceptance. A worker exit alone does
        # not turn an unverified job green or replace a coordinator failure.
        if item['queue_state']=='accepted':
            item['status']='running' if execution.get('phase')=='running' and node.get('status')=='live' else 'unknown' if node.get('status')!='live' else 'preparing' if execution.get('phase')=='completed' else 'retrying'
    elif item['queue_state']=='accepted' and node.get('status')!='live':item['status']='unknown'
    if node.get('status')!='live':
        item['detail']=(item.get('detail') or '')+' · Node unavailable; retained execution measurements are stale.'
    item.pop('report_keys',None);item.pop('task_id',None)
    return item


def snapshot():
    config=json.loads(CONFIG.read_text());name=config['name']
    if name!=config.get('master','compai.local'):raise ValueError('Queue API must run on its owner')
    queue=queue_snapshot(QUEUE,name);items=queue['items'];nodes={};errors=[];now=time.time()
    with LOCK:
        for host,address in list(config['peers'].items())[:24]:
            row=CACHE.setdefault(host,{'busy':False,'checked':0,'value':{'status':'loading'}})
            if not row['busy'] and time.monotonic()-row['checked']>=5:
                ids=[(i['task_id'],i['queue_state'] in ('completed','failed','cancelled')) for i in items if i.get('task_id') and (i.get('worker') or '').split('#')[0]==host]
                row['busy']=True
                POOL.submit(collect,host,address,row['value'],ids,host in config.get('wan_only',[]))
            value=dict(row['value'])
            if now-value.get('observed_at',0)>30 and value.get('status')=='live':value['status']='stale'
            nodes[host]=value
            if value.get('status')!='live':errors.append('Node '+value.get('status','unavailable')+': '+host)
    for value in nodes.values():
        index={}
        for record in value.get('records',{}).values():
            if record.get('controller_report'):
                key=report_key(record['controller_report'])
                if key not in index or record.get('observed_at',0)>index[key].get('observed_at',0):index[key]=record
        value['report_index']=index
    items=[merge_item(i,nodes.get((i.get('worker') or '').split('#')[0],{'status':'unavailable'})) for i in items]
    infrastructure=[i for n in nodes.values() for i in n.get('infrastructure',[]) if n.get('status')=='live']
    return {'sampled_at':now,'items':items,'infrastructure':infrastructure,'machines':sorted(config['peers']),
            'nodes':{h:{k:n.get(k) for k in ('status','observed_at')} for h,n in nodes.items()},
            'runtime_summary':runtime_summary(items),'errors':errors,
            'source':'Coordinator queue API joined with each owning node’s authenticated API'}
