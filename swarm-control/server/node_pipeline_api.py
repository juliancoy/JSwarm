"""Read-only pipeline data owned by this node, never a coordinator mirror.

The browser joins coordinator queue receipts with worker-owned observations.
Private enrollment material is used only for local agent calls and never returned.
"""
import datetime
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
import threading
import time
from urllib.parse import parse_qs

from agent_client import AgentClient
from machine_controls import project as control_state
from task_machines import project as machine_state

PAGE_SIZE = 512
WORKER_FIELDS = ('job','container','container_id','run','image','group','sink','cpus','memory_mb',
                 'status','running','exit_code','started_at','finished_at','oom_killed','observed_at',
                 'cpu_core_seconds','cpu_sampled_at','memory_peak_bytes','memory_current_bytes','memory_swap_bytes')


def report_key(path):
    return hashlib.sha256(str(path).encode()).hexdigest() if path else None


def journal_page(path, after=''):
    """Timestamp/id cursor includes ties and updates to existing attempts."""
    cursor = json.loads(after) if after else [0, '']
    if (not isinstance(cursor,list) or len(cursor)!=2 or type(cursor[0]) not in (int,float)
            or not math.isfinite(cursor[0]) or cursor[0]<0 or not isinstance(cursor[1],str) or len(cursor[1])>128):
        raise ValueError('Invalid node journal cursor')
    path=Path(path)
    if not path.is_file():return {'items':[],'cursor':cursor,'more':False,'available':False}
    with sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True,timeout=2) as db:
        rows=db.execute('SELECT job,updated,observation FROM workers WHERE updated>? OR (updated=? AND job>?) ORDER BY updated,job LIMIT ?',
                        (cursor[0],cursor[0],cursor[1],PAGE_SIZE+1)).fetchall()
    items=[]
    for job,updated,raw in rows[:PAGE_SIZE]:
        value=json.loads(raw)
        if value.get('job')!=job:raise ValueError('Worker journal identity differs')
        items.append({**{key:value[key] for key in WORKER_FIELDS if key in value},'report_key':report_key(value.get('controller_report'))})
        cursor=[updated,job]
    return {'items':items,'cursor':cursor,'more':len(rows)>PAGE_SIZE,'available':True}


def queue_snapshot(path, node):
    """Only queue-owned definitions, placement, dependencies and verified receipts.

    No worker telemetry database, remote files or node-status mirrors are read.
    """
    from task_pipeline import resources,run_type,runtime_summary
    path=Path(path)
    if not path.is_file():raise FileNotFoundError('Coordinator queue unavailable')
    items=[]
    with sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True,timeout=2) as db:
        db.row_factory=sqlite3.Row
        rows=db.execute('SELECT namespace,id,spec,state,host,created,updated,request,error,result FROM submissions ORDER BY created,id LIMIT 100001')
        for row in rows:
            if len(items)>=100000:raise ValueError('Pipeline queue envelope exceeded')
            spec=json.loads(row['spec']);request=json.loads(row['request'] or '{}');result=json.loads(row['result'] or '{}')
            request=request if isinstance(request,dict) else {};result=result if isinstance(result,dict) else {}
            submitted=resources(spec.get('resources'));placement=resources(request.get('resources')) if row['state'] in ('accepted','preparing') else None
            directory=request.get('directory')
            keys=[report_key(str(Path(directory)/name)) for name in ('execution.json','staging-execution.json')] if directory else []
            if request.get('report'):keys.append(report_key(request['report']))
            verified=bool(result.get('receipt_sha256') or result.get('primary_pair_accounting_verified') is True)
            items.append({'id':row['namespace']+'/'+row['id'],'job_id':row['id'],'parent':row['namespace'],
                'name':spec.get('name') or row['id'],'shard':spec.get('shard'),'chromosome':spec.get('chromosome'),
                'region':spec.get('region'),'stage':spec.get('stage'),'kind':spec.get('kind','work item'),
                'run_type':run_type(spec,request,row['namespace']),'queue_state':row['state'],
                'status':'preparing' if row['state']=='accepted' else row['state'],
                'worker':row['host'],'dependencies':spec.get('depends_on',spec.get('dependencies',[])),
                'resources':placement or submitted,'submission_resources':submitted,'placement_resources':placement,
                'worker_resources':None,'execution':None,'runtime':{},'report_keys':keys,
                'output_verified':verified,'completed_at':row['updated'] if row['state']=='completed' else None,
                'detail':row['error'],'detail_label':'Coordinator queue note','created':row['created'],'updated':row['updated'],
                'generation':spec.get('generation'),'reference_mode':None,'queue_node':node,
                'validation_identity':hashlib.sha256(json.dumps(spec,sort_keys=True,separators=(',',':')).encode()).hexdigest() if row['namespace'].startswith('intel-routing-smoke:') else None})
    # Repeated validation runs remain one logical test with intact attempt history.
    groups={};other=[]
    for item in items:
        family='application-test' if item['kind']=='application-test' else 'intel-routing-smoke' if item['validation_identity'] else None
        if family:groups.setdefault((family,item['job_id'],item['validation_identity']),[]).append(item)
        else:other.append(item)
    for (family,job,identity),attempts in groups.items():
        latest=dict(attempts[-1]);latest['id']=family+'/'+(identity[:16]+'/' if identity else '')+job
        latest['attempts']=[{k:a.get(k) for k in ('id','parent','status','queue_state','worker','created','updated','detail')} for a in reversed(attempts)]
        other.append(latest)
    return {'node':node,'source':'coordinator-owned queue and verification receipts','sampled_at':time.time(),
            'items':other,'runtime_summary':runtime_summary(other)}


def sample_cpu():
    def read():
        return {p[0]:list(map(int,p[1:9])) for line in Path('/proc/stat').read_text().splitlines()
                if (p:=line.split()) and p[0].startswith('cpu') and p[0][3:].isdigit()}
    before=read();time.sleep(.3);after=read();cores=[]
    for name,values in after.items():
        if name not in before:continue
        delta=[max(0,b-a) for a,b in zip(before[name],values)];total=sum(delta)
        if not total:continue
        user=100*(delta[0]+delta[1])/total;system=100*(delta[2]+delta[5]+delta[6])/total
        steal=100*delta[7]/total
        cores.append({'id':int(name[3:]),'user_percent':user,'system_percent':system,
                      'iowait_percent':100*delta[4]/total,'steal_percent':steal,'busy_percent':user+system+steal})
    return {'cores':cores,'sampled_at':time.time(),'interval_seconds':.3}


class NodePipeline:
    def __init__(self,config):
        self.config=config;self.lock=threading.Lock();self.sample=None;self.checked=0
        self.owner=Path(os.environ.get('LIFETECH_PEER_CONFIG',str(Path.home()/'.config/lifetech-launcher/peers.json'))).parent
        self.queue=Path(os.environ.get('JSWARM_QUEUE',str(Path.home()/'.local/state/swarm-control/submission/queue.sqlite')))

    def snapshot(self):
        with self.lock:
            if self.sample and time.monotonic()-self.checked<1:return self.sample
            name=self.config['name'];client=AgentClient({'host':name,'agent_endpoint':'http://'+self.config['peers'][name]+':8790'})
            try:
                status=client.call('status',timeout=5)
                if status.get('node')!=name:raise ValueError('Local agent identity differs')
                metrics=client.call('metrics',timeout=5)
                if metrics.get('host') not in (None,name):raise ValueError('Local telemetry identity differs')
            finally:client.close()
            from pipeline_workers import project as cache_workers
            machine=machine_state(name,status,metrics);machine.update(cpu=sample_cpu(),control=control_state(name,status),
                speed={'status':'unavailable','source':'Browser','target':name,'note':'Direct browser transfer measurement has not run yet.'})
            active=[]
            for worker in status.get('workers',[]):
                if worker.get('group')=='shared-cache':continue
                active.append({k:worker[k] for k in ('job','container','run','sink','group','cpus','memory_mb','resident_mb','active') if k in worker})
            self.sample={'node':name,'sampled_at':time.time(),'source':'this node’s local JSwarm API and worker journal',
                         'machine':machine,'active_workers':active,'infrastructure':cache_workers(name,status,time.time())}
            self.checked=time.monotonic();return self.sample

    def get(self,path,query=''):
        args=parse_qs(query,keep_blank_values=True)
        if path=='/api/node/snapshot':
            if args:raise ValueError('Snapshot accepts no arguments')
            return self.snapshot()
        if path=='/api/node/workers':
            if set(args)-{'after'} or any(len(v)!=1 for v in args.values()):raise ValueError('Invalid worker page')
            return {'node':self.config['name'],**journal_page(self.owner/'docker-worker-journal.sqlite',args.get('after',[''])[0])}
        if path=='/api/node/queue':
            if self.config['name']!=self.config['coordinator']:raise PermissionError('This node does not own the queue')
            if args:raise ValueError('Queue accepts no arguments')
            return queue_snapshot(self.queue,self.config['name'])
        raise KeyError(path)
