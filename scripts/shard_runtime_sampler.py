"""Persist execution timing and memory profiles outside executor-owned reports."""
import json
import sqlite3
import time
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parent))

ROOT=Path('/mnt/PILEDRIVE/genomics-workbench/wgs/recall/venter/generations')
DB=Path.home()/'.local/state/swarm-control/shard-runtime.sqlite'
QUEUE=Path.home()/'.local/state/swarm-control/submission/queue.sqlite'


def sample():
    active=[]
    DB.parent.mkdir(parents=True,exist_ok=True)
    with sqlite3.connect(DB,timeout=5) as db:
        db.execute('CREATE TABLE IF NOT EXISTS observations (job TEXT, container TEXT, report TEXT, PRIMARY KEY(job,container))')
        for path in ROOT.glob('*/full/SRR*.json'):
            try:
                if path.stat().st_size>1024*1024:continue
                report=json.loads(path.read_text());container=report.get('container')
                if not container:continue
                if report.get('phase')=='running':active.append((path.stem,report))
                prior=db.execute('SELECT report FROM observations WHERE job=? AND container=?',(path.stem,container)).fetchone()
                old=json.loads(prior[0]) if prior else {}
                # Preserve starts only within the same container/attempt.
                fields=('host','container','phase','started','finished','elapsed_seconds','gpu','sink','cpus','memory_mb','image','reference_transport','shared_cache','profiling_context','cpu_core_seconds','cpu_sampled_at','cpu_throttled_seconds','cpu_measurement_basis','cpu_accounting_coverage','queue_seconds','queue_timing_basis','admission_wait_seconds')
                fields+=('memory_current_bytes','memory_peak_bytes','memory_anon_bytes','memory_file_bytes','memory_shmem_bytes','memory_swap_bytes','memory_sampled_at','memory_measurement_basis','memory_accounting_coverage')
                value={**old,**{k:report[k] for k in fields if k in report and report[k] is not None}}
                if 'command' in report:value['shared_index']='LIFETECH_BWA_SHARED=1' in report['command']
                encoded=json.dumps(value,sort_keys=True)
                if not prior or encoded!=prior[0]:
                    db.execute('INSERT OR REPLACE INTO observations VALUES (?,?,?)',(path.stem,container,encoded))
            except (OSError,ValueError,TypeError):continue

    from shard_cpu_accounting import collect
    for job,container,counters in collect(active):
        with sqlite3.connect(DB,timeout=5) as db:
            prior=db.execute('SELECT report FROM observations WHERE job=? AND container=?',(job,container)).fetchone()
            if prior:
                value={**json.loads(prior[0]),**counters}
                if counters.get('memory_peak_bytes') is not None:value.pop('memory_measurement_invalid_reason',None)
                db.execute('UPDATE observations SET report=? WHERE job=? AND container=?',(json.dumps(value,sort_keys=True),job,container))


def sample_streams(limit=500,fetch=None):
    """Read bounded timing batches; keep diagnostics outside executor-owned data."""
    if not QUEUE.exists():return 0
    DB.parent.mkdir(parents=True,exist_ok=True)
    with sqlite3.connect(DB,timeout=5) as retained:
        retained.execute('CREATE TABLE IF NOT EXISTS stream_observations (namespace TEXT,id TEXT,host TEXT,task_id TEXT,report TEXT,PRIMARY KEY(namespace,id))')
        saved={(ns,ident):(host,task,json.loads(report)) for ns,ident,host,task,report in retained.execute('SELECT * FROM stream_observations')}
    with sqlite3.connect(QUEUE.resolve().as_uri()+'?mode=ro',uri=True,timeout=5) as queue:
        rows=queue.execute("SELECT namespace,id,host,task_id,state,result FROM submissions WHERE namespace LIKE 'stream:%' AND state IN ('accepted','completed','failed') AND task_id IS NOT NULL ORDER BY state='completed',id").fetchall()
    groups={};from_receipts=[]
    for ns,ident,host,task,state,encoded_result in rows:
        previous=saved.get((ns,ident))
        if state=='completed' and previous and previous[:2]==(host,task) and previous[2].get('phase')=='complete':continue
        try:receipt=json.loads(encoded_result or '{}')
        except (ValueError,TypeError):receipt={}
        if not isinstance(receipt,dict):receipt={}
        timing=receipt.get('execution')
        if state=='completed' and receipt.get('id')==task and receipt.get('state')=='completed' and isinstance(timing,dict) and timing.get('state')=='completed':
            report={**timing,'phase':'complete','host':host,'task_id':task}
            from_receipts.append((ns,ident,host,task,json.dumps(report,sort_keys=True)));continue
        lane=groups.setdefault(host,[])
        if len(lane)<limit:lane.append((ns,ident,task))
    def read(entry):
        host,assigned=entry
        try:
            if fetch is not None:data=fetch(host,[row[2] for row in assigned])
            else:
                sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'swarm-control/server'))
                from agent_client import AgentClient,CONFIG
                cfg=json.loads(CONFIG.read_text())
                if host not in cfg['peers']:return []
                client=AgentClient({'host':host,'agent_endpoint':'http://'+cfg['peers'][host]+':8790'})
                try:
                    data={}
                    for offset in range(0,len(assigned),100):
                        data.update(client.call('stream.timings',{'ids':[row[2] for row in assigned[offset:offset+100]]},timeout=15)['items'])
                finally:client.close()
            result=[]
            for ns,ident,task in assigned:
                timing=data.get(task)
                if not isinstance(timing,dict):continue
                phase={'completed':'complete','retry_wait':'retrying','admitting':'waiting_capacity','queued':'waiting_capacity','launching':'preparing'}.get(timing.get('state'),timing.get('state'))
                report={**timing,'phase':phase,'host':host,'task_id':task}
                result.append((ns,ident,host,task,json.dumps(report,sort_keys=True)))
            return result
        except (OSError,RuntimeError,ValueError,KeyError):return []
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=5) as pool:batches=list(pool.map(read,groups.items()))
    observations=from_receipts+[row for batch in batches for row in batch]
    with sqlite3.connect(DB,timeout=5) as retained:
        retained.executemany('INSERT OR REPLACE INTO stream_observations VALUES (?,?,?,?,?)',observations)
    return len(observations)


FOLLOWUP_RUNTIME=Path.home()/'.local/state/swarm-control/task-runtime/julian-huref-existing-data-followup'


def sample_followup(fetch=None,runtime_root=None):
    """Measure accepted follow-up attempts without changing execution records."""
    if not QUEUE.exists():return 0
    runtime_root=Path(runtime_root) if runtime_root is not None else FOLLOWUP_RUNTIME
    with sqlite3.connect(QUEUE.resolve().as_uri()+'?mode=ro',uri=True,timeout=5) as queue:
        rows=queue.execute("SELECT namespace,id,host,state,request FROM submissions WHERE (namespace LIKE 'genomics-followup:%' OR namespace LIKE 'genomics-followup-inputs:%') AND state IN ('accepted','completed','failed') AND request IS NOT NULL").fetchall()
    DB.parent.mkdir(mode=0o700,parents=True,exist_ok=True);active=[];saved={}
    with sqlite3.connect(DB,timeout=5) as db:
        db.execute('CREATE TABLE IF NOT EXISTS observations (job TEXT, container TEXT, report TEXT, PRIMARY KEY(job,container))')
        for namespace,ident,host,state,encoded in rows:
            try:
                request=json.loads(encoded);path=Path(request['directory'])/('extraction.json' if namespace.startswith('genomics-followup-inputs:') else 'execution.json')
                if path.stat().st_size>1024**2:continue
                report=json.loads(path.read_text());container=report.get('container')
                if not container or report.get('host')!=host:continue
                key=namespace+'#'+ident
                previous=db.execute('SELECT report FROM observations WHERE job=? AND container=?',(key,container)).fetchone()
                old=json.loads(previous[0]) if previous else {}
                fields=('host','container','phase','started','finished','elapsed_seconds','timing_basis','sink','cpus','memory_mb','memory_reservation_mb','image','profiling_context','cpu_core_seconds','cpu_sampled_at','cpu_throttled_seconds','cpu_measurement_basis','cpu_accounting_coverage','queue_seconds','queue_timing_basis','admission_wait_seconds')
                fields+=('memory_current_bytes','memory_peak_bytes','memory_anon_bytes','memory_file_bytes','memory_shmem_bytes','memory_swap_bytes','memory_sampled_at','memory_measurement_basis','memory_accounting_coverage')
                value={**old,**{k:report[k] for k in fields if k in report},'namespace':namespace,'id':ident,'attempt_directory':str(path.parent),'recorded_at':path.stat().st_mtime}
                db.execute('INSERT OR REPLACE INTO observations VALUES(?,?,?)',(key,container,json.dumps(value,sort_keys=True)))
                saved[(key,container)]=value
                if report.get('phase')=='running' and state=='accepted':active.append((key,report))
            except (OSError,ValueError,KeyError,TypeError):continue
    from shard_cpu_accounting import collect
    measurements=(fetch or collect)(active)
    runtime_root.mkdir(mode=0o700,parents=True,exist_ok=True)
    with sqlite3.connect(DB,timeout=5) as db:
        for key,container,counters in measurements:
            if (key,container) not in saved:continue
            value={**saved[(key,container)],**counters}
            if counters.get('memory_peak_bytes') is not None:value.pop('memory_measurement_invalid_reason',None)
            db.execute('UPDATE observations SET report=? WHERE job=? AND container=?',(json.dumps(value,sort_keys=True),key,container))
            memory=counters.get('memory_current_bytes');peak=counters.get('memory_peak_bytes')
            summary={'sampled_at':counters.get('memory_sampled_at',time.time()),'cpu_seconds':counters.get('cpu_core_seconds'),
                'producer_memory':{k:v for k,v in counters.items() if k.startswith('memory_')},
                'runtime_detail':('Cgroup RAM '+str(round(memory/1024**2,1))+' MiB; observed peak '+str(round(peak/1024**2,1))+' MiB; private reservation '+str(value.get('memory_reservation_mb'))+' MiB.') if memory is not None and peak is not None else 'Awaiting worker cgroup RAM sample'}
            path=runtime_root/(value['id']+'.json');temporary=path.with_suffix('.tmp');temporary.write_text(json.dumps(summary));temporary.chmod(0o600);temporary.replace(path)
    return len(measurements)


def sample_finalizer(root=None,fetch=None):
    """Project existing finalizer receipts; never submit or adopt execution here."""
    import re
    root=Path(root) if root is not None else ROOT.parent/'finalize'
    try:
        state=json.loads((root/'state.json').read_text());generation=state['generation']
        if not re.fullmatch('[a-f0-9]{12}',generation):return 0
    except (OSError,ValueError,KeyError):return 0
    folder=root/'generations'/generation;rows=[];active=[]
    fields=('host','container','phase','started','finished','elapsed_seconds','sink','cpus','memory_mb','memory_reservation_mb','image','cpu_core_seconds','cpu_sampled_at','cpu_throttled_seconds','cpu_measurement_basis','cpu_accounting_coverage','queue_seconds','queue_timing_basis','admission_wait_seconds')
    fields+=('memory_current_bytes','memory_peak_bytes','memory_anon_bytes','memory_file_bytes','memory_shmem_bytes','memory_swap_bytes','memory_sampled_at','memory_measurement_basis','memory_accounting_coverage')
    with sqlite3.connect(DB,timeout=5) as db:
        db.execute('CREATE TABLE IF NOT EXISTS finalizer_observations (generation TEXT,id TEXT,report TEXT,PRIMARY KEY(generation,id))')
        previous={ident:json.loads(report) for ident,report in db.execute('SELECT id,report FROM finalizer_observations WHERE generation=?',(generation,))}
    specs=[(run,folder/'mate-prepared'/(run+'-execution.json'),run in state.get('mate_prepared',{}))
           for run in state.get('collected',{}) if re.fullmatch('SRR7097858-'+generation+'-[0-9]{6}',run)]
    specs.append(('global-merge',folder/'merge.json',state.get('primary_pair_accounting_verified') is True))
    for ident,path,verified in specs:
        report={};old=previous.get(ident,{})
        try:
            stat=path.stat()
            if stat.st_size>1024**2 or path.is_symlink():continue
            report=json.loads(path.read_text())
        except (OSError,ValueError):pass
        value={**(old if old.get('container')==report.get('container') else {}),
               **{k:report[k] for k in fields if k in report},'recorded_at':path.stat().st_mtime if report else None,
               'host':report.get('host') or state.get('target_host'),'verified':verified,'sampled_at':time.time()}
        terminal=report.get('container_state') or {}
        if not value.get('finished') and terminal.get('FinishedAt','').startswith('20'):value['finished']=terminal['FinishedAt']
        value['status']='completed' if verified else {'running':'running','complete':'preparing','failed':'failed','interrupted':'interrupted'}.get(report.get('phase'),'waiting_dependency' if ident=='global-merge' else 'queued')
        if value['status']=='completed':value['phase']='complete'
        elif value['status']=='running' and report.get('container'):active.append((ident,report))
        rows.append((ident,value))
    from shard_cpu_accounting import collect
    measured={ident:(container,counters) for ident,container,counters in (fetch or collect)(active)}
    for ident,value in rows:
        observation=measured.get(ident)
        if observation and observation[0]==value.get('container'):value.update(observation[1])
        if value['status']=='running':
            recent=max(value.get('cpu_sampled_at') or 0,value.get('memory_sampled_at') or 0,value.get('recorded_at') or 0)
            if time.time()-recent>120:value['status']='unknown'
    metadata={'host':state.get('target_host'),'phase':state.get('phase'),'sampled_at':time.time(),
              'prepared':len(state.get('mate_prepared',{})),'total':len(state.get('collected',{}))}
    with sqlite3.connect(DB,timeout=5) as db:
        db.executemany('INSERT OR REPLACE INTO finalizer_observations VALUES(?,?,?)',
                       [(generation,ident,json.dumps(value)) for ident,value in [*rows,('__workflow__',metadata)]])
    return len(rows)


def invalidate_helper_memory():
    """Do not retain historical helper-slice values as worker memory peaks."""
    if not DB.exists():return 0
    invalid='container cgroup v2; file includes shmem, do not add them; shared cache owner charged separately'
    changed=0
    with sqlite3.connect(DB,timeout=10) as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='observations'").fetchone():return 0
        for job,container,raw in db.execute('SELECT job,container,report FROM observations').fetchall():
            report=json.loads(raw)
            if report.get('memory_measurement_basis')!=invalid:continue
            report={k:v for k,v in report.items() if not k.startswith('memory_')}
            report['memory_measurement_invalid_reason']='Historical owner-helper cgroup was sampled; worker RAM was not measured'
            db.execute('UPDATE observations SET report=? WHERE job=? AND container=?',(json.dumps(report,sort_keys=True),job,container));changed+=1
    return changed


if __name__=='__main__':
    import argparse
    parser=argparse.ArgumentParser();parser.add_argument('--once',action='store_true');parser.add_argument('--stream-limit',type=int,default=500);args=parser.parse_args()
    if not 1<=args.stream_limit<=10000:parser.error('--stream-limit must be 1..10000')
    invalidate_helper_memory()
    while True:
        sample()
        sample_streams(args.stream_limit)
        sample_followup()
        sample_finalizer()
        if args.once:break
        time.sleep(15)
