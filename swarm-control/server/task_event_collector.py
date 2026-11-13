"""Continuously retain available snapshots and source events, independently of UI."""
import hashlib
import json
import logging
from pathlib import Path
import time
from task_events import EventStore, encode
from paths import STATE


def import_run(store, value, source):
    store.observe(value,'application-tests',source)
    records=[]
    for i, event in enumerate(value.get('activity', [])):
        key='historical-activity:'+value['id']+':'+hashlib.sha256(encode(event).encode()).hexdigest()
        records.append((value['id'], source,'historical-activity',event,key))
    for host in value.get('hosts', []):
        for name, job in host.get('jobs', {}).items():
            # Retained logs are imported as observations, never invented original events.
            for i, line in enumerate(job.get('logs', [])):
                event={'host':host['host'],'suite':name,'retained_index':i,'line':line}
                key='retained-log:'+value['id']+':'+hashlib.sha256(encode(event).encode()).hexdigest()
                records.append((value['id'],host['host'],'historical-retained-log',event,key))
    store.append_batch(records)


def collect_once(store=None, state=STATE):
    store=store or EventStore()
    with store.connect() as db:
        canonical={row[0] for row in db.execute("SELECT id FROM runs WHERE category='application-tests'")}
    for path in (state/'distributed').glob('*.json'):
        try:
            source='run-file:'+str(path)
            stamp=path.stat().st_mtime_ns
            # Repair older shadowed projections even when the executor file
            # itself has not changed since the previous collection.
            if store.cursor(source)==stamp and path.stem in canonical:continue
            value=json.loads(path.read_text())
            if value.get('id'):import_run(store,value,'retained-run-file')
            with store.connect() as db:
                db.execute('INSERT INTO cursors VALUES(?,?) ON CONFLICT(source) DO UPDATE SET offset=excluded.offset',(source,stamp))
        except (ValueError,OSError,KeyError):logging.exception('Cannot import run %s',path.name)
    for path in (state/'tasks').glob('*.json'):
        try:store.observe(json.loads(path.read_text()),'registry','registry-file')
        except (ValueError,OSError):logging.exception('Cannot import task %s',path.name)
    for root in (Path.home()/'.local/state/venter-peer-qc').glob('venter-qc-*'):
        path=root/'events.jsonl';source='peer-jsonl:'+__import__('socket').gethostname()+':'+root.name
        if path.exists():
            with path.open() as stream:
                lines=[]
                offset=store.cursor(source)
                for i,line in enumerate(stream,1):
                    if i>offset:
                        try:lines.append((i,json.loads(line)))
                        except ValueError:break # A partially written last line is retried.
                store.ingest_lines(source,root.name,lines)
        for path in (root/'results').glob('*/receipt.json'):
            try:
                value=json.loads(path.read_text());key='peer-receipt:'+root.name+':'+path.parent.name
                store.append(root.name,'signed-peer-receipt','receipt-observed',value,key)
            except (OSError,ValueError):logging.exception('Cannot import receipt')


WORKER_ROOTS=['.local/state/lifetech-launcher/jobs','.local/share/organization-node/.runtime/launcher-jobs','Documents/Organization/.runtime/launcher-jobs']


def outbox_code(offsets):
    return """import pathlib,sqlite3,json
roots="""+repr(WORKER_ROOTS)+"""
offsets="""+repr(offsets)+"""
result=[]
for index,root in enumerate(roots):
 path=pathlib.Path.home()/root/'test-events.sqlite3'
 if not path.is_file():continue
 db=sqlite3.connect('file:'+str(path)+'?mode=ro',uri=True,timeout=10)
 try:rows=db.execute('SELECT rowid,job,sequence,payload,recorded_at FROM events WHERE rowid>? ORDER BY rowid LIMIT 1000',(offsets[index],)).fetchall();result.append([index,rows])
 finally:db.close()
print(json.dumps(result))
"""


def ingest_outboxes(store,node,execute):
    sources=['worker-outbox:'+node+':'+str(i) for i in range(len(WORKER_ROOTS))]
    offsets=[store.cursor(s) for s in sources]
    for index,rows in execute(outbox_code(offsets)):
        store.ingest_worker(sources[index],node,rows)


def collect_input_progress(store,node,execute):
    from paths import WORKSPACE
    code=(WORKSPACE/'swarm-control/server/input_progress.py').read_text()
    rows=execute(code)
    folder=STATE/'input-progress';folder.mkdir(exist_ok=True,parents=True,mode=0o700)
    for row in rows:
        path=folder/(row['job']+'.json')
        previous=json.loads(path.read_text()) if path.exists() else {}
        elapsed=row['sampled_at']-previous.get('sampled_at',row['sampled_at'])
        delta=row['completed_units']-previous.get('completed_units',row['completed_units'])
        if previous.get('container')==row['container'] and 0<elapsed<120 and delta>0:
            row['units_per_second']=delta/elapsed
            row['read_eta_seconds']=(row['total_units']-row['completed_units'])/row['units_per_second']
        row['worker']=node
        temporary=path.with_suffix('.tmp');temporary.write_text(json.dumps(row));temporary.chmod(0o600);temporary.replace(path)


def collect_peer_journal(store,node,run,execute):
    source='peer-sqlite:'+node+':'+run;offset=store.cursor(source)
    code="""import pathlib,sqlite3,json
p=pathlib.Path.home()/'.local/state/venter-peer-qc'/"""+repr(run)+"""/'retries.sqlite3'
rows=[]
if p.exists():
 db=sqlite3.connect('file:'+str(p)+'?mode=ro',uri=True,timeout=10)
 try:rows=[(i,json.loads(s)) for i,s in db.execute('SELECT sequence,payload FROM events WHERE sequence>? ORDER BY sequence LIMIT 1000',("""+str(offset)+""",))]
 finally:db.close()
print(json.dumps(rows))
"""
    store.ingest_lines(source,run,execute(code))


def collect_peers(store):
    """Read public QC event logs over already enrolled, host-pinned SSH only."""
    import sys,subprocess,shlex
    from concurrent.futures import ThreadPoolExecutor
    from paths import WORKSPACE
    sys.path.insert(0,str(WORKSPACE))
    from docker_utils.policy import inventory,ssh
    runs=[p.name for p in (Path.home()/'.local/state/venter-peer-qc').glob('venter-qc-*') if p.is_dir()]
    def visit(host):
        def execute(code):
            result=subprocess.run(ssh(host)+[shlex.join(['python3','-c',code])],capture_output=True,text=True,timeout=12)
            if result.returncode:raise RuntimeError('Cannot read worker outbox')
            return json.loads(result.stdout)
        collect_input_progress(store,host['host'],execute)
        ingest_outboxes(store,host['host'],execute)
        for run in runs:
            collect_peer_journal(store,host['host'],run,execute)
            source='peer-jsonl:'+host['host'].removesuffix('.local')+':'+run
            offset=store.cursor(source)
            code="import pathlib,json; p=pathlib.Path.home()/'.local/state/venter-peer-qc'/"+repr(run)+"/'events.jsonl'; print(json.dumps([(i,json.loads(s)) for i,s in enumerate(p.open(),1) if i>"+str(offset)+"]))"
            result=subprocess.run(ssh(host)+[shlex.join(['python3','-c',code])],capture_output=True,text=True,timeout=12)
            if result.returncode==0:
                store.ingest_lines(source,run,json.loads(result.stdout))
    local_node=json.loads((Path.home()/'.config/lifetech-launcher/peers.json').read_text())['name']
    def execute_local(code):
        return json.loads(subprocess.check_output([sys.executable,'-c',code],text=True,timeout=12))
    collect_input_progress(store,local_node,execute_local)
    ingest_outboxes(store,local_node,execute_local)
    for run in runs:collect_peer_journal(store,local_node,run,execute_local)
    with ThreadPoolExecutor(max_workers=5) as pool:
        futures=[pool.submit(visit,h) for h in inventory()]
        for future in futures:
            try:future.result()
            except Exception:logging.warning('Peer event source unavailable; retrying next cycle')


def run():
    store=EventStore();last_peer=0;last_retention=0
    while True:
        try:
            if time.monotonic()-last_retention>300:
                store.retain_history();last_retention=time.monotonic()
            collect_once(store)
            if time.monotonic()-last_peer>30:
                collect_peers(store);last_peer=time.monotonic()
        except Exception:logging.exception('Compute event collection failed')
        time.sleep(5)
