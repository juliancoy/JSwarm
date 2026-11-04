"""Durable bounded retries of already-published application test jobs."""
import hashlib
import json
from pathlib import Path
import re
import time
from task_events import EventStore,encode


class RetryQueue:
    def __init__(self,store=None):
        self.store=store or EventStore()
        with self.store.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS retry_jobs(id TEXT PRIMARY KEY,parent_run TEXT NOT NULL,task TEXT NOT NULL,attempts INTEGER NOT NULL,state TEXT NOT NULL,next_due REAL,reason TEXT,child_run TEXT,updated REAL NOT NULL)')
    def offer(self,parent,task,attempt=1,blocked=False,reason=None):
        family=parent.get('retry_family',parent['id']);ident=family+':'+task
        with self.store.connect() as db:
            state='blocked' if blocked else 'exhausted' if attempt>=3 else 'backoff'
            delay=30*2**max(0,attempt-1);jitter=.8+int(hashlib.sha256(ident.encode()).hexdigest()[:4],16)/65535*.4
            due=time.time()+delay*jitter if state=='backoff' else None
            db.execute('INSERT OR IGNORE INTO retry_jobs VALUES(?,?,?,?,?,?,?,?,?)',(ident,parent['id'],task,attempt,state,due,reason,None,time.time()))
    def claim(self,now=None):
        rows=self.claim_batch(1,now)
        return rows[0] if rows else None
    def claim_batch(self,limit=32,now=None):
        if type(limit) is not int or not 1<=limit<=64:raise ValueError('Invalid retry batch size')
        now=time.time() if now is None else now
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            # Only admission claims expire; an actual child run is reconciled, not redispatched.
            db.execute("UPDATE retry_jobs SET state='backoff',next_due=? WHERE state='claiming' AND updated<?",(now,now-120))
            row=db.execute("SELECT * FROM retry_jobs WHERE state='backoff' AND next_due<=? ORDER BY next_due LIMIT 1",(now,)).fetchone()
            if not row:return []
            rows=db.execute("SELECT * FROM retry_jobs WHERE state='backoff' AND next_due<=? AND parent_run=? ORDER BY next_due LIMIT ?",(now,row[1],limit)).fetchall()
            db.executemany("UPDATE retry_jobs SET state='claiming',updated=? WHERE id=?",[(now,r[0]) for r in rows])
            return [dict(zip(('id','parent_run','task','attempts','state','next_due','reason','child_run','updated'),r)) for r in rows]
    def dispatched(self,ident,child):
        self.dispatched_batch([ident],child)
    def dispatched_batch(self,idents,child):
        with self.store.connect() as db:
            db.executemany("UPDATE retry_jobs SET state='running',child_run=?,attempts=attempts+1,next_due=NULL,reason=NULL,updated=? WHERE id=? AND state='claiming'",[(child,time.time(),ident) for ident in idents])
    def defer(self,ident,reason):
        with self.store.connect() as db:db.execute("UPDATE retry_jobs SET state='backoff',next_due=?,reason=?,updated=? WHERE id=? AND state='claiming'",(time.time()+30,reason[:300],time.time(),ident))
    def block(self,ident,reason):
        with self.store.connect() as db:db.execute("UPDATE retry_jobs SET state='blocked',next_due=NULL,reason=?,updated=? WHERE id=?",(reason[:300],time.time(),ident))
    def finish(self,ident,success,blocked=False,reason=None):
        with self.store.connect() as db:
            row=db.execute('SELECT attempts FROM retry_jobs WHERE id=?',(ident,)).fetchone()
            if not row:return
            state='completed' if success else 'blocked' if blocked else 'exhausted' if row[0]>=3 else 'backoff'
            due=time.time()+min(900,30*2**(row[0]-1)) if state=='backoff' else None
            db.execute('UPDATE retry_jobs SET state=?,next_due=?,reason=?,updated=? WHERE id=?',(state,due,reason,time.time(),ident))
    def rows(self):
        with self.store.connect() as db:
            return [dict(zip(('id','parent_run','task','attempts','state','next_due','reason','child_run','updated'),row)) for row in db.execute('SELECT * FROM retry_jobs ORDER BY updated DESC')]


def blocked(job):
    return job.get('exit_code') in (126,127,137) or job.get('status') in ('stopped','conflict')

def unexecuted(parent):
    if parent.get('status')!='failed' or parent.get('stop_requested'):return []
    catalog=set(parent.get('task_catalog',[]))
    passed={key for host in parent.get('hosts',[]) for key,job in host.get('jobs',{}).items() if job.get('status') in ('passed','reused')}
    return [task for task in parent.get('pending_tasks',[]) if task in catalog and task not in passed and task.split('--',1)[0] not in ('p2p','mobile','gpu')]


def retained_parent(store, folder, ident):
    """Recover only the original executor's fingerprint-verified retry input."""
    if not isinstance(ident,str) or not re.fullmatch(r'[A-Za-z0-9_-]+',ident):
        raise ValueError('Invalid pinned parent identity')
    try:parent=json.loads((Path(folder)/(ident+'.json')).read_text())
    except (OSError,ValueError) as error:
        raise ValueError('Pinned parent record unavailable; restore the retained executor record') from error
    from github_sources import source_digest
    if (parent.get('id')!=ident or not parent.get('repositories')
            or parent.get('source_sha256')!=source_digest(parent['repositories'])):
        raise ValueError('Retained pinned parent source fingerprint differs')
    from task_event_collector import import_run
    import_run(store,parent,'retry-parent-recovery')
    return parent


def run(service):
    queue=RetryQueue();store=queue.store
    # Existing historical failures are retained, not silently rerun months later.
    with store.connect() as db:
        db.execute('CREATE TABLE IF NOT EXISTS retry_settings(key TEXT PRIMARY KEY,value TEXT NOT NULL)')
        db.execute("INSERT OR IGNORE INTO retry_settings VALUES('enabled_since',?)",(str(time.time()),))
        enabled=float(db.execute("SELECT value FROM retry_settings WHERE key='enabled_since'").fetchone()[0])
        db.execute("INSERT OR IGNORE INTO retry_settings VALUES('unexecuted_enabled_since',?)",(str(time.time()),))
        unexecuted_enabled=float(db.execute("SELECT value FROM retry_settings WHERE key='unexecuted_enabled_since'").fetchone()[0])
    while True:
        try:
            with service.lock:
                available=not service.active() and not (service.runner.folder/'DRAIN').exists()
            records={r['id']:r for r in store.list_runs('application-tests')}
            for row in queue.rows():
                if row['state']=='running' and row['child_run'] in records:
                    child=records[row['child_run']]
                    if child.get('finished'):
                        jobs=[j for h in child.get('hosts',[]) for k,j in h.get('jobs',{}).items() if k==row['task']]
                        success=any(j.get('status') in ('passed','reused') for j in jobs)
                        # A worker can fail before receiving the selected task.
                        # Such a child cannot describe the next pinned retry.
                        queue.finish(row['id'],success,any(blocked(j) for j in jobs),None if success else 'Retry failed; history retained')
                        store.append(row['parent_run'],'retry-system','retry-completed' if success else 'retry-failed',{'task':row['task'],'child_run':child['id'],'attempt':row['attempts']},'retry-result:'+child['id']+':'+row['task'])
            for parent in records.values():
                if not parent.get('finished') or parent.get('retry_family') or not parent.get('repositories') or parent.get('stop_requested'):continue
                from datetime import datetime
                if datetime.fromisoformat(parent['finished']).timestamp()<enabled:continue
                verified={key for host in parent.get('hosts',[]) for key,job in host.get('jobs',{}).items() if job.get('status') in ('passed','reused')}
                for host in parent.get('hosts',[]):
                    for task,job in host.get('jobs',{}).items():
                        if task in verified or task in ('p2p','mobile','gpu') or job.get('status') not in ('failed','timed_out','unavailable','interrupted'):continue
                        queue.offer(parent,task,blocked=blocked(job),reason='Failed on '+host['host'])
                if parent.get('recover_pending') or datetime.fromisoformat(parent['finished']).timestamp()>=unexecuted_enabled:
                    for task in unexecuted(parent):queue.offer(parent,task,attempt=0,reason='Not executed before worker preparation or transport failure')
            if available:
                tickets=queue.claim_batch()
                if tickets:
                    ticket={**tickets[0],'batch':tickets}
                    try:
                        parent=records.get(ticket['parent_run'])
                        if not parent:parent=retained_parent(store,service.runner.folder,ticket['parent_run'])
                        runner=service.retry_pinned(parent,ticket)
                        queue.dispatched_batch([t['id'] for t in tickets],runner.run['id'])
                        store.append(parent['id'],'retry-system','retry-dispatched',{'tasks':[t['task'] for t in tickets],'child_run':runner.run['id'],'batch_size':len(tickets)})
                        __import__('threading').Thread(target=runner.execute,daemon=True).start()
                    except Exception as error:
                        reason=type(error).__name__+': '+str(error)
                        for t in tickets:
                            if isinstance(error,ValueError):queue.block(t['id'],reason)
                            else:queue.defer(t['id'],reason)
        except Exception:__import__('logging').exception('Retry admission deferred')
        time.sleep(5)
