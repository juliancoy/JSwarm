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
    def status(self,ident):
        """Poll once, validating terminal results without blocking assignment."""
        row=self.call('stream.status',{'id':ident})
        if row['state']=='completed':
            result=row['result']
            if result.get('contract_sha256')!=row['contract_sha256'] or result.get('worker_machine')!=self.host['machine']:raise IntegrityFailure('Launcher result identity differs')
        if row['state']=='failed_integrity':raise IntegrityFailure(row['error'])
        if row['state'] in ('failed','cancelled'):raise WorkFailure(row['error'])
        return row
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
    def claim(self,host,capacity=None,requirements=None):
        row=self.queue.claim(host,capacity,requirements=requirements)
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


def distribute(units,hosts,prepare,journal,*,stopped=lambda:False,progress=lambda value:None,minimum_resources=None):
    path=Path(journal);path.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
    with path.with_suffix(path.suffix+'.lock').open('a+') as lock:
        try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:raise RuntimeError('This fleet queue already has a controller') from None
        return _distribute(units,hosts,prepare,journal,stopped=stopped,progress=progress,minimum_resources=minimum_resources)


def _distribute(units,hosts,prepare,journal,*,stopped=lambda:False,progress=lambda value:None,minimum_resources=None):
    """Assign from live capacity while polling accepted jobs independently.

    Each host has one control connection, with no fixed execution slot count.
    Pending admissions reserve CPU/RAM in the durable queue until the launcher
    reports running; the node remains responsible for final resource admission.
    """
    units=list(units)
    minimum_resources=minimum_resources or {'cpus':1,'memory_mb':256}
    requirements={u['id']:{k:u.get('resources',{}).get(k,minimum_resources.get(k,default))
        for k,default in (('cpus',1),('memory_mb',256))} for u in units}
    queue=FleetQueue(journal,units);queue.recover_preparations();fatal=threading.Event()
    recovered_by_host={host['host']:queue.recover(host['host']) for host in hosts}
    progress_lock=threading.Lock();last_progress=[float('-inf')]
    def report(force=False):
        with progress_lock:
            now=time.monotonic()
            if force or now-last_progress[0]>=5:
                progress(queue.snapshot());last_progress[0]=now
    report()
    def run(host):
        try:
            with StreamClient(host,lambda:stopped() or fatal.is_set()) as client:
                if not client.capabilities():raise WorkFailure('Launcher '+host['host']+' lacks stream-work-v1; accepted work retained for recovery')
                active={};closed=False
                def checkpoint(ident,spec,pending):
                    queue.update(ident,state='accepted',task_id=spec['id'],request=canonical({
                        'spec':spec,'resources':spec.get('resources',{'cpus':1,'memory_mb':256}),
                        'admission_pending':pending}))
                for row in recovered_by_host[host['host']]:
                    request=json.loads(row['request']);spec=request.get('spec',request)
                    # Idempotent submission also handles a lost acknowledgement.
                    client.submit(spec);checkpoint(row['id'],spec,True)
                    active[row['id']]=(spec,True)
                while not stopped() and not fatal.is_set():
                    report()
                    for ident,(spec,pending) in list(active.items()):
                        try:
                            result=client.status(spec['id'])
                            if result['state']=='completed':
                                queue.update(ident,state='completed',result=canonical(result),error=None)
                                del active[ident];report(force=True)
                            else:
                                admission_pending=result['state']!='running'
                                if admission_pending!=pending:
                                    checkpoint(ident,spec,admission_pending)
                                    active[ident]=(spec,admission_pending)
                        except IntegrityFailure as error:
                            queue.update(ident,state='failed_integrity',error=str(error));fatal.set();report(force=True);return
                        except WorkFailure as error:
                            queue.update(ident,state='failed',error=str(error));del active[ident];report(force=True)
                    if fatal.is_set():return
                    row=None
                    if not closed and queue.snapshot().get('queued'):
                        closed=not client.call('worker.policy')['accepting_jobs']
                        if not closed:
                            available=client.call('resource.capacity')
                            row=queue.claim(host['host'],available,requirements=requirements)
                    if row is not None:
                        try:spec=prepare(json.loads(row['spec']),host)
                        except Exception as error:
                            queue.update(row['id'],state='queued',host=None,error=str(error));raise
                        # Persist the full immutable request before contacting
                        # the launcher, so uncertain acceptance is recoverable.
                        checkpoint(row['id'],spec,True)
                        try:client.submit(spec)
                        except WorkerPaused:
                            queue.update(row['id'],state='queued',host=None,task_id=None,request=None);closed=True
                        else:active[row['id']]=(spec,True)
                        continue
                    if not active and (closed or not queue.snapshot().get('queued')):return
                    time.sleep(.25)
        except InterruptedError:
            if not fatal.is_set() and not stopped():
                fatal.set();raise
        except Exception:
            fatal.set();raise
        finally:report(force=True)
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1,len(hosts))) as pool:
        for future in [pool.submit(run,h) for h in hosts]:future.result()
    result=queue.snapshot()
    if result.get('failed_integrity'):raise IntegrityFailure('Fleet work contains a rejected input or result')
    return result
