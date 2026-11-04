"""Read-only shard projection of the shared submission queue."""
import datetime
import statistics
import json
import math
import re
from pathlib import Path
import sqlite3
import time
from runtime_estimates import attach_estimates

QUEUE=Path.home()/'.local/state/swarm-control/submission/queue.sqlite'
RUNTIME_DB=Path.home()/'.local/state/swarm-control/shard-runtime.sqlite'


def collapse_test_attempts(items):
    """One current state per logical test; prior queue observations stay inspectable."""
    tests={};result=[]
    for item in items:
        if item['kind']!='application-test':
            result.append(item)
            continue
        tests.setdefault(item['job_id'],[]).append(item)
    for task,attempts in tests.items():
        attempts.sort(key=lambda item:(item['created'],item['updated'],item['id']))
        latest=dict(attempts[-1])
        latest['attempt_id']=latest['id']
        latest['id']='application-test/'+task
        latest['attempts']=[{key:item.get(key) for key in
                            ('id','parent','status','queue_state','worker','created','updated','detail')}
                           for item in reversed(attempts)]
        result.append(latest)
    return result


WGS=Path('/mnt/PILEDRIVE/genomics-workbench/wgs')


def resources(value):
    if not isinstance(value,dict):return None
    result={key:value[key] for key in ('cpus','memory_mb') if isinstance(value.get(key),(int,float)) and not isinstance(value[key],bool) and math.isfinite(value[key]) and value[key]>0}
    if value.get('sink') in ('cpu','gpu'):result['sink']=value['sink']
    return result or None


def execution_report(namespace,ident,host,root=WGS):
    # Local, bounded reads for active alignments only. No SSH, Docker inspection,
    # fleet polling, or filesystem scans on dashboard requests.
    if not namespace.startswith('venter-alignment:'):return None
    generation=namespace.split(':',1)[1]
    if not re.fullmatch(r'[a-f0-9]{12}',generation) or not re.fullmatch(r'SRR7097858-'+generation+r'-[0-9]{6}',ident):return None
    path=root/'recall/venter/generations'/generation/'full'/(ident+'.json')
    try:
        if path.stat().st_size>1024*1024:return None
        report=json.loads(path.read_text())
        if str(report.get('host','')).lower()!=str(host or '').split('#')[0].lower():return None
        retained=Path.home()/'.local/state/swarm-control/shard-runtime.sqlite'
        if retained.exists():
            try:
                with sqlite3.connect(retained.resolve().as_uri()+'?mode=ro',uri=True,timeout=.2) as db:
                    saved=db.execute('SELECT report FROM observations WHERE job=? AND container=?',(ident,report.get('container'))).fetchone()
                    if saved:report={**json.loads(saved[0]),**report}
            except (sqlite3.Error,OSError,ValueError):pass
        io=None
        try:
            io_path=path.with_name(ident+'.io.json')
            if io_path.stat().st_size<=128*1024:
                candidate=json.loads(io_path.read_text())
                if candidate.get('container')==report.get('container') and candidate.get('worker')==report.get('host'):io=candidate
        except (OSError,ValueError,AttributeError):pass
        return {**{k:report[k] for k in ('cpu_core_seconds','cpu_throttled_seconds','cpu_sampled_at','cpu_measurement_basis','memory_current_bytes','memory_peak_bytes','memory_anon_bytes','memory_file_bytes','memory_shmem_bytes','memory_swap_bytes','memory_sampled_at','memory_measurement_basis','profiling_context') if k in report},'io':io,'phase':report.get('phase'),'resources':resources(report),'recorded_at':path.stat().st_mtime,
                'container':report.get('container'), 'reference_transport':report.get('reference_transport'),
                'shared_index':'LIFETECH_BWA_SHARED=1' in report.get('command',''),
                'shared_reference_ram':bool(report.get('shared_cache')),
                'execution_owner':report.get('execution_owner'),'node_job_id':report.get('node_job_id'),
                'alignment_backend':report.get('alignment_backend'),'reference_location':report.get('reference_location'),
                'gpu_index_resident':report.get('gpu_index_resident'),
                'started':report.get('started'),'finished':report.get('finished'),
                'elapsed_seconds':report.get('elapsed_seconds'),'gpu':bool(report.get('gpu')),
                'gpu_devices':[str(report['gpu'].get('name') or report['gpu'].get('uuid'))[:200]] if isinstance(report.get('gpu'),dict) and (report['gpu'].get('name') or report['gpu'].get('uuid')) else [],
                'sink':report.get('sink'),'image':report.get('image')}
    except (OSError,ValueError,TypeError):return None


def io_observation(value, result, worker, state):
    """Publish bounded, validated per-shard payload counters, never host totals."""
    value=value if isinstance(value,dict) else {}
    directions={}
    for direction in ('read','write'):
        entries=value.get(direction)
        if not isinstance(entries,list):
            directions[direction]=None
            continue
        grouped={}
        for entry in entries[:256]:
            if not isinstance(entry,dict):continue
            size=entry.get('bytes');endpoint=entry.get('endpoint')
            if type(size) is not int or size<0 or size>2**63 or not isinstance(endpoint,str) or not endpoint:continue
            key=(endpoint[:200],entry.get('network') is True)
            grouped[key]=grouped.get(key,0)+size
        directions[direction]=[{'endpoint':endpoint,'bytes':size,'network':network} for (endpoint,network),size in grouped.items()]
    # Old receipts have exact verified output sizes, but no retained read counters.
    historical=False
    if directions['write'] is None and state=='completed' and isinstance(result.get('outputs'),dict):
        sizes=[v.get('bytes') for v in result['outputs'].values() if isinstance(v,dict)]
        if sizes and all(type(n) is int and 0<=n<=2**63 for n in sizes) and result.get('host'):
            directions['write']=[{'endpoint':str(result['host'])[:200],'bytes':sum(sizes),
                                  'network':str(result['host']).lower()!=str(result.get('executor') or worker or '').split('#')[0].lower()}]
            historical=True
    read=directions['read'];write=directions['write']
    return {**directions,'read_bytes':sum(e['bytes'] for e in read) if read is not None else None,
            'write_bytes':sum(e['bytes'] for e in write) if write is not None else None,
            'network_read_bytes':sum(e['bytes'] for e in read if e['network']) if read is not None else None,
            'network_write_bytes':sum(e['bytes'] for e in write if e['network']) if write is not None else None,
            'state':value.get('state') if isinstance(value.get('state'),str) else ('Recorded outputs only' if historical else 'Not measured'),
            'sampled_at':value.get('sampled_at') if type(value.get('sampled_at')) in (int,float) and math.isfinite(value['sampled_at']) else None,
            'coverage':'Verified payload bytes only. Excludes mapped reference/cache reads, scratch I/O, protocol overhead, retries and unfinished transfers. Not physical disk or total wire I/O.'}


def archive_benchmark_attempts(items,root):
    """Keep superseded experiments inspectable without claiming they passed."""
    try:
        path=root/'qc/full-shard-memory-pressure/latest.json'
        if path.stat().st_size>1024*1024:return
        current=json.loads(path.read_text())
        ident=current.get('id','')
        if not re.fullmatch(r'[0-9]{8}T[0-9]{6}Z-[a-f0-9]{6}',ident):return
        if current.get('phase') not in ('staging_inputs_in_ram','waiting_for_existing_node_work','benchmarking','complete'):return
        parent='memory-pressure:'+ident
        for item in items:
            if (item['kind']=='memory-pressure-benchmark' and item['parent']!=parent
                    and item['shard']==current.get('shard') and item['worker']==current.get('target')
                    and item['queue_state'] in ('failed','completed')):
                item['superseded_by']=parent
                item['history_reason']='Previous benchmark attempt; replacement sweep '+ident+' · '+current['phase'].replace('_',' ')+'. Original outcome retained.'
    except (OSError,ValueError,TypeError,AttributeError):return


def failure_detail(spec,result,state):
    if spec.get('kind')!='memory-pressure-benchmark':return None
    if state=='completed' and result.get('outcome')=='memory-pressure-timeout':return 'Benchmark measurement completed: memory-pressure timeout. The alignment did not finish; retained counters describe the tested resource constraint.'
    if state!='failed':return None
    limit=result.get('limit_mib') or (spec.get('resources') or {}).get('memory_mb')
    if result.get('interrupted'):return 'Benchmark interrupted for the reporting repair; prior outputs retained. The repaired sweep is a separate run.'
    if result.get('timed_out'):
        elapsed=result.get('wall_seconds') or result.get('duration_seconds')
        duration=f' after {elapsed:.0f}s' if type(elapsed) in (int,float) and math.isfinite(elapsed) else ''
        return f'Memory-pressure experiment reached its time limit{duration} at {limit} MiB. Historical pressure counters were not retained; this is an inconclusive benchmark trial, not a failed production alignment.'
    code=(result.get('container_state') or {}).get('ExitCode')
    return f'Memory-pressure benchmark terminated (exit {code}) at {limit} MiB; no verified alignment was accepted.'


def run_type(spec,request,namespace):
    recipe=str(request.get('recipe') or spec.get('recipe') or '').lower()
    kind=str(spec.get('kind') or '').lower()
    if recipe.startswith('fastq.') and 'qc' in recipe or kind in ('genetic-qc','genomic-qc','fastq-qc'):return 'genetic-qc'
    if kind in ('paired-read-alignment','read-alignment','mapping') or namespace.startswith('venter-alignment:'):return 'mapping'
    if kind=='application-test' or namespace.startswith('application-tests:'):return 'web'
    return 'other'


def completion_time(execution,result,fallback):
    nested=result.get('result') if isinstance(result.get('result'),dict) else {}
    for value in ((execution or {}).get('finished'),result.get('finished'),nested.get('finished'),fallback):
        if isinstance(value,str):
            try:value=datetime.datetime.fromisoformat(value.replace('Z','+00:00')).timestamp()
            except ValueError:continue
        if type(value) in (int,float) and math.isfinite(value) and value>0:return value
    return None


def stream_observations(path):
    if not path.exists():return {}
    try:
        with sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True,timeout=.5) as db:
            return {(ns,ident):(host,task,json.loads(report)) for ns,ident,host,task,report in db.execute('SELECT * FROM stream_observations')}
    except (sqlite3.Error,OSError,ValueError):return {}


def enrolled_machines(config=None):
    """Enrollment is independent of workload filters and never polls nodes."""
    if config is None:
        from agent_client import CONFIG
        config=CONFIG
    try:
        peers=json.loads(Path(config).read_text()).get('peers',{})
        return sorted(peers) if isinstance(peers,dict) else []
    except (OSError,ValueError,TypeError):return []


def snapshot(path=QUEUE,report_root=WGS,runtime_path=RUNTIME_DB):
    if not path.exists():return {'sampled_at':time.time(),'items':[],'machines':enrolled_machines(),'errors':[]}
    items=[];stream_reports=stream_observations(runtime_path)
    with sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True,timeout=2) as db:
        db.row_factory=sqlite3.Row
        task_column='task_id' if any(column[1]=='task_id' for column in db.execute('PRAGMA table_info(submissions)')) else 'NULL AS task_id'
        for row in db.execute('SELECT namespace,id,spec,state,host,created,updated,request,error,result,'+task_column+' FROM submissions ORDER BY created,id'):
            spec=json.loads(row['spec']);request=json.loads(row['request'] or '{}')
            if not isinstance(request,dict):request={}
            state=row['state'];phase=state
            if state=='accepted':
                phase='waiting_capacity' if request.get('admission_pending') is True or 'admission_pending' not in request and 'capacity' in (row['error'] or '').lower() else 'preparing'
            elif state=='preparing':phase='preparing'
            submitted=resources(spec.get('resources'))
            placement=resources(request.get('resources')) if state in ('preparing','accepted') else None
            execution=execution_report(row['namespace'],row['id'],row['host'],report_root) if state in ('accepted','completed','failed') and request.get('admission_pending') is not True else None
            if row['namespace'].startswith('stream:'):
                saved=stream_reports.get((row['namespace'],row['id']))
                if saved and saved[:2]==(row['host'],row['task_id']):
                    execution=saved[2]
                    if state=='accepted':phase=execution.get('phase',phase)
            worker_resources=None
            if execution and execution['container']:
                if execution['phase']=='running':phase='running';worker_resources=execution['resources']
                elif execution['phase'] in ('failed','interrupted'):phase='retrying'
            try:result=json.loads(row['result'] or '{}')
            except (TypeError,ValueError):result={}
            if not isinstance(result,dict):result={}
            peak=result.get('peak_memory_bytes') if state=='completed' else None
            if not isinstance(peak,(int,float)) or isinstance(peak,bool) or not math.isfinite(peak) or peak<=0:peak=None
            dependencies=spec.get('depends_on',spec.get('dependencies',[]))
            if not isinstance(dependencies,list):dependencies=[str(dependencies)]
            items.append({'id':row['namespace']+'/'+row['id'],'job_id':row['id'],'parent':row['namespace'],
                'name':spec.get('name') or ('Shard '+str(spec['shard']) if 'shard' in spec else row['id']),
                'shard':spec.get('shard'),'kind':spec.get('kind','work item'),'run_type':run_type(spec,request,row['namespace']),'completed_at':completion_time(execution,result,row['updated']) if state=='completed' else None,'status':phase,'queue_state':state,
                'worker':row['host'],'dependencies':dependencies,'resources':worker_resources or placement or submitted,
                'submission_resources':submitted,'placement_resources':placement,'worker_resources':worker_resources,
                'execution':{k:v for k,v in execution.items() if k!='io'} if execution else None,'peak_memory_bytes':peak,
                'profiling_context':result.get('profiling_context') or (execution or {}).get('profiling_context'),
                'output_verified':result.get('primary_pair_accounting_verified') is True,
                'configuration_equivalent':(result.get('configuration_comparison') or {}).get('baseline_equivalent'),
                'io':io_observation(result.get('io') if state=='completed' else (execution or {}).get('io'),result,row['host'],state),
                'runtime':runtime_observation(execution,result,row['host']),
                'work_units':result.get('pairs') or spec.get('reads'),
                'detail':row['error'] or failure_detail(spec,result,state),'detail_label':'Prior queue note (before worker started)' if execution and execution['phase']=='running' and execution['recorded_at']>row['updated'] else 'Benchmark outcome' if spec.get('kind')=='memory-pressure-benchmark' and not row['error'] and failure_detail(spec,result,state) else 'Last queue note','created':row['created'],'updated':row['updated'],
                'reference_mode':result.get('reference_transport') or (execution or {}).get('reference_transport') or ('shared-index' if (execution or {}).get('shared_index') else request.get('reference_mode') if state in ('preparing','accepted') else None),'generation':spec.get('generation')})
    # A stream workflow's recipe can be retained only on admitted rows. Apply
    # its unambiguous run type to queued siblings in that same workflow.
    workflow_types={}
    for item in items:
        if item['run_type']!='other':workflow_types.setdefault(item['parent'],set()).add(item['run_type'])
    for item in items:
        types=workflow_types.get(item['parent'],set())
        if item['run_type']=='other' and item['parent'].startswith('stream:') and len(types)==1:item['run_type']=next(iter(types))
    archive_benchmark_attempts(items,report_root)
    items=collapse_test_attempts(items)
    inference=attach_estimates(items)
    return {'sampled_at':time.time(),'items':items,'machines':enrolled_machines(),'runtime_summary':runtime_summary(items),'runtime_inference':inference,'errors':[]}


def runtime_observation(execution, result, worker):
    report=execution or {}
    elapsed=report.get('elapsed_seconds') or result.get('elapsed_seconds') or result.get('wall_seconds') or result.get('duration_seconds')
    try:
        start=datetime.datetime.fromisoformat(report['started']).timestamp()
        end=datetime.datetime.fromisoformat(report['finished']).timestamp() if report.get('finished') else time.time()
        elapsed=end-start
    except (KeyError,TypeError,ValueError):pass
    if type(elapsed) not in (int,float) or not math.isfinite(elapsed) or elapsed<=0:elapsed=None
    backend=result.get('alignment_backend') or report.get('alignment_backend') or {}
    transport=result.get('reference_transport') or report.get('reference_transport')
    nested=result.get('result') if isinstance(result.get('result'),dict) else {}
    outputs=nested.get('outputs') if isinstance(nested.get('outputs'),dict) else {}
    gpu_bases=outputs.get('gpu_bases')
    qc_gpu=type(gpu_bases) in (int,float) and math.isfinite(gpu_bases) and gpu_bases>0
    gpu_used=bool(qc_gpu or report.get('gpu') or backend.get('gpu') or backend.get('backend')=='bwa-vulkan-exact')
    devices=outputs.get('gpu_devices') if qc_gpu else report.get('gpu_devices') or []
    if not isinstance(devices,list):devices=[]
    devices=[name[:200] for name in devices[:16] if isinstance(name,str) and name]
    descriptor=backend.get('gpu')
    if not devices and isinstance(descriptor,dict) and (descriptor.get('name') or descriptor.get('uuid')):devices=[str(descriptor.get('name') or descriptor['uuid'])[:200]]
    technologies=[]
    if qc_gpu:
        technologies.append('GPU quality control')
        if outputs.get('histogram_source')=='vulkan' or outputs.get('qc_backend')=='vulkan':technologies.append('Vulkan')
        if outputs.get('gpu_cpu_crosschecked_batches',0):technologies.append('CPU cross-check')
    elif backend.get('backend')=='bwa-vulkan-exact':technologies.append('GPU alignment (CPU seeding)')
    elif report.get('gpu') or backend.get('gpu'):technologies.append('GPU (scope unreported)')
    elif report.get('sink')=='cpu' or backend.get('backend')=='bwa':technologies.append('CPU')
    if transport=='nfs-fscache':technologies.append('FS-CACHE reference')
    if transport=='node-shared-ram' or report.get('shared_reference_ram'):
        technologies.append('Shared BWA index (host RAM)')
    elif report.get('shared_index'):
        technologies.append('Shared BWA index (file mapping)')
    if backend.get('backend'):technologies.append(backend['backend'])
    if result.get('execution_owner')=='jswarm-node' or report.get('execution_owner')=='jswarm-node':technologies.append('JSwarm node')
    core=report.get('cpu_core_seconds')
    if type(core) not in (int,float) or not math.isfinite(core) or core<0:core=None
    cpu_wall=elapsed
    try:
        cpu_wall=report['cpu_sampled_at']-datetime.datetime.fromisoformat(report['started']).timestamp()
    except (KeyError,TypeError,ValueError):pass
    return {'cpu_core_seconds':core,'average_cores':core/cpu_wall if core is not None and cpu_wall and cpu_wall>0 else None,
            'cpu_sampled_at':report.get('cpu_sampled_at'),'cpu_throttled_seconds':report.get('cpu_throttled_seconds'),
            'cpu_measurement_basis':report.get('cpu_measurement_basis'),'machine':worker,'elapsed_seconds':elapsed,'started':report.get('started'),
            'finished':report.get('finished'),'technologies':technologies,'gpu_used':gpu_used,'gpu_devices':devices,
            'image':report.get('image') or backend.get('image'),
            'timing_basis':report.get('timing_basis','execution report') if elapsed else 'unavailable; historical start was not retained'}


def runtime_summary(items, now=None):
    """Forecast submitted backlog from recent fleet completions, including lane contention.

    Queue residence is never used as a runtime sample. Completion throughput is
    a fallback for older attempts whose executor discarded their start time.
    """
    now=time.time() if now is None else now
    items=[i for i in items if not i.get('superseded_by')]
    pending=[i for i in items if i['status']!='completed']
    forecasts=[];unknown=[]
    blocked=[i for i in pending if i['status'] in ('failed','interrupted','partial','unknown','error','timed_out')]
    for parent in sorted({i['parent'] for i in pending}):
        jobs=[i for i in items if i['parent']==parent]
        remaining=[i for i in jobs if i['status']!='completed' and i not in blocked]
        if any(i in blocked for i in jobs):unknown.append(parent)
        if not remaining:continue
        done=sorted(i['updated'] for i in jobs if i['status']=='completed' and now-3600<=i['updated']<=now)
        if len(done)>=3 and done[-1]>done[0]:
            rate=(len(done)-1)/(done[-1]-done[0])
            gaps=sorted(b-a for a,b in zip(done,done[1:]))
            seconds=len(remaining)/rate
            low=min(seconds,len(remaining)*gaps[int((len(gaps)-1)*.1)])
            high=max(seconds,len(remaining)*gaps[int((len(gaps)-1)*.9)])
            forecasts.append({'workflow':parent,'remaining_jobs':len(remaining),
                              'seconds':seconds,'low_seconds':low,'high_seconds':high,'samples':len(done),
                              'basis':'Fleet completion throughput over the last hour'})
        elif parent not in unknown:unknown.append(parent)
    seconds=max((f['seconds'] for f in forecasts),default=0 if not pending else None)
    low=max((f['low_seconds'] for f in forecasts),default=seconds)
    high=max((f['high_seconds'] for f in forecasts),default=seconds)
    groups={}
    for item in items:
        observation=item.get('runtime') or {}
        key=(observation.get('machine'),tuple(observation.get('technologies',[])))
        group=groups.setdefault(key,{'machine':key[0],'technologies':list(key[1]),'completed_samples':0,'seconds':[]})
        if item['status']=='completed' and observation.get('elapsed_seconds'):
            group['completed_samples']+=1;group['seconds'].append(observation['elapsed_seconds'])
    for group in groups.values():
        values=group.pop('seconds');group['median_seconds']=statistics.median(values) if values else None
    return {'remaining_jobs':len(pending),'estimated_seconds':seconds,
            'sampled_at':now,'arrival_at':now+seconds if seconds is not None else None,
            'arrival_low_at':now+low if low is not None else None,'arrival_high_at':now+high if high is not None else None,
            'blocked_jobs':len(blocked),'estimated_jobs':sum(f['remaining_jobs'] for f in forecasts),
            'complete_coverage':not unknown,'unestimated_workflows':unknown,'workflows':forecasts,
            'scope':'Submitted backlog only. Latest concurrent workflow forecast at observed fleet throughput; assumes current capacity shares. Range uses empirical 10–90% completion gaps, not a confidence interval. Failed jobs require recovery. Future shards and unsubmitted downstream work excluded.',
            'groups':list(groups.values())}
