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
                fields=('host','container','phase','started','finished','elapsed_seconds','gpu','sink','cpus','memory_mb','image','reference_transport','shared_cache','profiling_context')
                value={**old,**{k:report[k] for k in fields if k in report}}
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


if __name__=='__main__':
    import argparse
    parser=argparse.ArgumentParser();parser.add_argument('--once',action='store_true');parser.add_argument('--stream-limit',type=int,default=500);args=parser.parse_args()
    if not 1<=args.stream_limit<=10000:parser.error('--stream-limit must be 1..10000')
    while True:
        sample()
        sample_streams(args.stream_limit)
        if args.once:break
        time.sleep(15)
