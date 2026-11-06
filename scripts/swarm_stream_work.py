"""Common client and durable fleet queue for launcher streaming recipes."""
import concurrent.futures
import contextlib
import fcntl
import http.client
import json
import os
import hashlib
import uuid
from pathlib import Path
import sqlite3
import re
import threading
import time
from scripts.launcher_streams import canonical,digest,TERMINAL
from scripts.swarm_runtime import AgentClient


class IntegrityFailure(RuntimeError):pass
class WorkFailure(RuntimeError):pass
class WorkerPaused(RuntimeError):pass


def byte_ranges(size,target_bytes=64*1024*1024):
    """Raw-byte partitions only. Compressed and scientific boundaries are app-owned."""
    if type(size) is not int or type(target_bytes) is not int or size<0 or target_bytes<1:raise ValueError('Invalid byte partition')
    for start in range(0,size,target_bytes):yield {'start':start,'length':min(target_bytes,size-start)}


class StreamClient:
    def __init__(self,host,stopped=lambda:False,client=None):
        self.host=host;self.stopped=stopped;self.client=client or AgentClient(host)
    def call(self,op,args=None,*,deadline=None):
        while not self.stopped():
            if deadline is not None and time.monotonic()>=deadline:raise TimeoutError('Wait ended; launcher retains accepted streaming work')
            try:return self.client.call(op,args or {},**({'timeout':max(.1,min(30,deadline-time.monotonic()))} if deadline is not None else {}))
            except (OSError,http.client.HTTPException):
                self.client.close()
                if deadline is not None and time.monotonic()>=deadline:raise TimeoutError('Wait ended; launcher retains accepted streaming work') from None
                time.sleep(min(1,max(0,deadline-time.monotonic())) if deadline is not None else 1)
        raise InterruptedError('Streaming controller stopped; accepted work remains on its launcher')
    def capabilities(self):
        status=self.call('status')
        if status['machine']!=self.host['machine']:raise ValueError('Enrolled streaming node identity changed')
        if 'stream-work-v1' not in status.get('capabilities',[]):return None
        return self.call('stream.capabilities')
    def submit(self,spec):
        # stream.submit is immutable/idempotent, including a lost acknowledgement.
        try:return self.call('stream.submit',spec)
        except RuntimeError as error:
            if 'Worker paused' in str(error):raise WorkerPaused('Worker paused before accepting new streaming work') from error
            raise
    def prioritize(self,ident,priority=100):
        return self.call('stream.priority',{'id':ident,'priority':priority})
    def wait(self,ident,progress=lambda row:None,timeout=None):
        started=time.monotonic()
        while not self.stopped():
            row=self.call('stream.status',{'id':ident},deadline=started+timeout if timeout is not None else None);progress(row)
            if row['state']=='completed':
                result=row['result']
                if result.get('contract_sha256')!=row['contract_sha256'] or result.get('worker_machine')!=self.host['machine']:raise IntegrityFailure('Launcher result identity differs')
                return row
            if row['state']=='failed_integrity':raise IntegrityFailure(row['error'])
            if row['state'] in ('failed','cancelled'):raise WorkFailure(row['error'])
            if timeout is not None and time.monotonic()-started>=timeout:raise TimeoutError('Wait ended; launcher retains the accepted streaming task')
            time.sleep(2)
        raise InterruptedError('Controller stopped; launcher work retained')
    def run(self,spec,progress=lambda row:None,timeout=None):
        row=self.submit(spec)
        if row['state']=='completed':
            if row['result'].get('contract_sha256')!=row['contract_sha256'] or row['result'].get('worker_machine')!=self.host['machine']:raise IntegrityFailure('Cached streaming worker identity differs')
            return row
        return self.wait(spec['id'],progress,timeout)
    def close(self):self.client.close()
    def __enter__(self):return self
    def __exit__(self,*args):self.close()


class FleetQueue:
    """Streaming adapter onto the coordinator submission queue.

    Existing journals are imported transactionally before any claims. Local node
    journals remain the authority for already accepted execution and results.
    """
    def __init__(self,path,units):
        from submission_queue import SubmissionQueue
        self.path=Path(path)
        shared=os.getenv('SWARM_SUBMISSION_QUEUE')
        namespace='stream:'+hashlib.sha256(str(self.path.resolve()).encode()).hexdigest()
        self.queue=SubmissionQueue(namespace,shared)
        self.tokens={}
        with self.queue.connect() as db:
            imported=db.execute('SELECT 1 FROM submission_imports WHERE namespace=?',(namespace,)).fetchone()
        if self.path.exists() and not imported:
            with sqlite3.connect(self.path) as legacy:
                legacy.row_factory=sqlite3.Row
                if legacy.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='units'").fetchone():
                    old=list(legacy.execute('SELECT * FROM units'))
                    with self.queue.connect() as db:
                        self.queue.begin(db)
                        for row in old:
                            db.execute("INSERT OR IGNORE INTO submissions(namespace,id,fingerprint,spec,state,host,token,created,updated,task_id,request,result,error) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                                (namespace,row['id'],row['fingerprint'],row['spec'],row['state'],row['host'],uuid.uuid4().hex,time.time(),time.time(),row['task_id'],row['request'],row['result'],row['error']))
                        db.execute('INSERT OR IGNORE INTO submission_imports(namespace) VALUES(?)',(namespace,))
        self.queue.submit_many(units)
    def claim(self,host,capacity=None):
        row=self.queue.claim(host,capacity)
        if row:self.tokens[row['id']]=row['token']
        return row
    def recover(self,host):
        rows=self.queue.recover(host)
        for row in rows:self.tokens[row['id']]=row['token']
        return rows
    def update(self,ident,**fields):self.queue.update(ident,self.tokens[ident],**fields)
    def recover_preparations(self):self.queue.recover_preparations()
    def snapshot(self):return self.queue.snapshot()
    def workers(self):
        with self.queue.connect() as db:
            workers={}
            for name,state,count in db.execute('SELECT host,state,count(*) FROM submissions WHERE namespace=? AND host IS NOT NULL GROUP BY host,state',(self.queue.namespace,)):
                workers.setdefault(name,{})[state]=count
            return workers


def distribute(units,hosts,prepare,journal,*,slots=None,stopped=lambda:False,progress=lambda value:None,minimum_resources=None):
    path=Path(journal);path.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
    with path.with_suffix(path.suffix+'.lock').open('a+') as lock:
        try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:raise RuntimeError('This fleet queue already has a controller') from None
        return _distribute(units,hosts,prepare,journal,slots=slots,stopped=stopped,progress=progress,minimum_resources=minimum_resources)


def _distribute(units,hosts,prepare,journal,*,slots=None,stopped=lambda:False,progress=lambda value:None,minimum_resources=None):
    """Reusable work stealing; accepted jobs reattach to their original launchers.

    prepare(unit, host) builds a declared recipe request with that node's pinned
    image and source offers. It never sends code or shell commands to the launcher.
    Pass resource-derived slots for CPU/RAM-heavy jobs; the node rechecks admission.
    """
    if slots is None:
        # Every job reserves at least one CPU. This is the hardware upper bound
        # for feeder waiters, not a fixed per-node execution limit; launchers
        # independently admit declared CPU/RAM budgets.
        slots={h['host']:max(1,int(h.get('cpus',1))) for h in hosts}
    minimum_resources=minimum_resources or {'cpus':1,'memory_mb':0}
    queue=FleetQueue(journal,units);queue.recover_preparations();guard=threading.Lock();fatal=threading.Event()
    recovered_by_host={host['host']:queue.recover(host['host']) for host in hosts}
    progress_lock=threading.Lock();last_progress=[float('-inf')]
    def report(force=False):
        # Waiting for capacity and long accepted jobs are live states too.
        # Serialize callbacks and bound durable/UI writes across all lanes.
        with progress_lock:
            now=time.monotonic()
            if force or now-last_progress[0]>=5:
                progress(queue.snapshot());last_progress[0]=now
    report()
    def run(host,index):
        with StreamClient(host,lambda:stopped() or fatal.is_set()) as client:
            if not client.capabilities():raise WorkFailure('Launcher '+host['host']+' lacks stream-work-v1; accepted work retained for recovery')
            recovered=recovered_by_host[host['host']][index::(slots or {}).get(host['host'],1)]
            while not stopped() and not fatal.is_set():
                report()
                row=recovered.pop(0) if recovered else None
                if row is None:
                    if not client.call('worker.policy')['accepting_jobs']:return
                    if not queue.snapshot().get('queued'):return
                    available=client.call('resource.capacity')
                    if (available.get('available_cpus',float('inf'))<minimum_resources.get('cpus',1)
                            or available.get('available_mb',float('inf'))<minimum_resources.get('memory_mb',0)):
                        time.sleep(.25);continue
                    with guard:row=queue.claim(host['host'],available)
                    if row is None:
                        if not queue.snapshot().get('queued'):return
                        time.sleep(.25);continue
                    try:spec=prepare(json.loads(row['spec']),host)
                    except Exception as error:
                        queue.update(row['id'],state='queued',host=None,error=str(error));raise
                    queue.update(row['id'],state='accepted',task_id=spec['id'],request=canonical(spec))
                else:spec=json.loads(row['request'])
                try:
                    result=client.run(spec,progress=lambda _:report())
                    queue.update(row['id'],state='completed',result=canonical(result),error=None)
                except WorkerPaused:
                    queue.update(row['id'],state='queued',host=None,task_id=None,request=None);return
                except IntegrityFailure as error:
                    queue.update(row['id'],state='failed_integrity',error=str(error));fatal.set();return
                except WorkFailure as error:
                    queue.update(row['id'],state='failed',error=str(error));return
                except InterruptedError:
                    # Accepted work stays pinned to its launcher for later reattachment.
                    if not fatal.is_set() and not stopped():raise
                    return
                finally:report(force=True)
    work=[(h,n) for h in hosts for n in range((slots or {}).get(h['host'],1))]
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1,len(work))) as pool:
        for future in [pool.submit(run,h,n) for h,n in work]:future.result()
    result=queue.snapshot()
    if result.get('failed_integrity'):raise IntegrityFailure('Fleet work contains a rejected input or result')
    return result
