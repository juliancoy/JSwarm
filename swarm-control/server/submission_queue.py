"""One durable coordinator queue; launchers retain only accepted execution journals.

All adapters use this database on the coordinator's local filesystem, never an
SQLite file shared over LAN. Claims are atomic and fenced. A disconnected worker
is NOT evidence that an accepted job stopped: accepted claims never expire.
"""
import contextlib
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
import time
import uuid


def queue_path():
    return Path(os.environ.get('SWARM_SUBMISSION_QUEUE', str(Path.home()/'.local/state/swarm-control/submission/queue.sqlite')))


class SubmissionQueue:
    def __init__(self, namespace, path=None, *, epoch=None):
        self.epoch=epoch
        if epoch is not None and (not isinstance(epoch,str) or len(epoch)!=12 or any(c not in '0123456789abcdef' for c in epoch)):raise ValueError('Invalid controller epoch')
        self.namespace=namespace;self.path=Path(path) if path is not None else queue_path()
        self.path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
        with self.connect() as db:
            db.execute('PRAGMA journal_mode=WAL')
            db.execute('''CREATE TABLE IF NOT EXISTS submissions(
                namespace TEXT NOT NULL,id TEXT NOT NULL,fingerprint TEXT NOT NULL,
                spec TEXT NOT NULL,state TEXT NOT NULL,host TEXT,token TEXT,
                created REAL NOT NULL,updated REAL NOT NULL,task_id TEXT,request TEXT,
                result TEXT,error TEXT,PRIMARY KEY(namespace,id))''')
            if 'priority' not in {r[1] for r in db.execute('PRAGMA table_info(submissions)')}:db.execute('ALTER TABLE submissions ADD COLUMN priority INTEGER NOT NULL DEFAULT 0')
            db.execute('CREATE TABLE IF NOT EXISTS submission_imports(namespace TEXT PRIMARY KEY)')
            db.execute('CREATE INDEX IF NOT EXISTS submission_ready ON submissions(namespace,state,created,id)')
        self.path.chmod(0o600)

    @contextlib.contextmanager
    def connect(self):
        db=sqlite3.connect(self.path,timeout=30);db.row_factory=sqlite3.Row
        db.execute('PRAGMA synchronous=FULL')
        try:
            with db:yield db
        finally:db.close()

    @staticmethod
    def begin(db):
        # WAL has one writer. Disk pressure must cause backpressure, not turn a
        # valid job into a failed task after SQLite's busy timeout.
        while True:
            try:db.execute('BEGIN IMMEDIATE');return
            except sqlite3.OperationalError as error:
                if getattr(error,'sqlite_errorcode',None) not in (sqlite3.SQLITE_BUSY,sqlite3.SQLITE_LOCKED):raise
                time.sleep(.05)

    def submit(self,spec):self.submit_many([spec])

    def submit_many(self,specs):
        rows=[]
        for spec in specs:
            encoded=json.dumps(spec,sort_keys=True,separators=(',',':'),allow_nan=False)
            fingerprint=hashlib.sha256(encoded.encode()).hexdigest();ident=spec['id']
            if not isinstance(ident,str) or not ident:raise ValueError('Missing submission ID')
            resources=spec.get('resources',{})
            for key in ('cpus','memory_mb'):
                value=resources.get(key,0)
                if not isinstance(value,(int,float)) or isinstance(value,bool) or value<0 or not math.isfinite(value):raise ValueError('Invalid '+key)
            if resources.get('sink','cpu') not in ('cpu','gpu'):raise ValueError('Invalid job sink')
            priority=spec.get('priority',0)
            if type(priority) is not int or not -1000<=priority<=1000:raise ValueError('Invalid priority')
            rows.append((ident,fingerprint,encoded,priority))
        if not rows:return
        with self.connect() as db:
            self.begin(db)
            now=time.time()
            for ident,fingerprint,encoded,priority in rows:
                old=db.execute('SELECT fingerprint FROM submissions WHERE namespace=? AND id=?',(self.namespace,ident)).fetchone()
                if old and old['fingerprint']!=fingerprint:raise ValueError('Submission ID already identifies different inputs')
                db.execute('INSERT OR IGNORE INTO submissions(namespace,id,fingerprint,spec,state,created,updated,priority) VALUES(?,?,?,?,?,?,?,?)',(self.namespace,ident,fingerprint,encoded,'queued',now,now,priority))

    def prioritize(self,ident,priority=100):
        if type(priority) is not int or not -1000<=priority<=1000:raise ValueError('Invalid priority')
        with self.connect() as db:
            self.begin(db)
            if db.execute('UPDATE submissions SET priority=?,updated=? WHERE namespace=? AND id=?',(priority,time.time(),self.namespace,ident)).rowcount!=1:raise KeyError(ident)

    def claim(self,host,capacity=None,eligible=None,requirements=None):
        """Skip nonfitting jobs, without letting a large/GPU job block CPU work."""
        capacity=dict(capacity or {});eligible=None if eligible is None else set(eligible)
        if capacity.get('accepting_jobs') is False:return None
        with self.connect() as db:
            self.begin(db)
            physical=host.split('#',1)[0]
            for pending in db.execute("SELECT host,request FROM submissions WHERE state IN ('preparing','accepted') AND request IS NOT NULL"):
                if not pending['host'] or pending['host'].split('#',1)[0]!=physical:continue
                try:request=json.loads(pending['request'])
                except ValueError:continue
                if request.get('admission_pending') is not True:continue
                reserved=request['resources']
                if capacity.get('cpu_admission_mode')!='timeshare':capacity['available_cpus']=capacity.get('available_cpus',float('inf'))-reserved.get('cpus',0)
                capacity['available_mb']=capacity.get('available_mb',float('inf'))-reserved.get('memory_mb',0)
            for row in db.execute("SELECT * FROM submissions WHERE namespace=? AND state='queued' ORDER BY priority DESC,created,id",(self.namespace,)):
                if eligible is not None and row['id'] not in eligible:continue
                spec=json.loads(row['spec']);resources=spec.get('resources',{})
                estimate=(requirements or {}).get(row['id'],{})
                if set(estimate)-{'cpus','memory_mb'}:raise ValueError('Placement estimates may only change CPU/RAM budgets')
                for value in estimate.values():
                    if not isinstance(value,(int,float)) or isinstance(value,bool) or not math.isfinite(value) or value<=0:raise ValueError('Invalid placement resource estimate')
                resources={**resources,**estimate}
                if resources.get('cpus',0)>capacity.get('available_cpus',float('inf')):continue
                if resources.get('memory_mb',0)>capacity.get('available_mb',float('inf')):continue
                if resources.get('sink','cpu')=='gpu' and not capacity.get('gpu',False):continue
                if not set(resources.get('capabilities',[]))<=set(capacity.get('capabilities',[])):continue
                token=(self.epoch+':' if self.epoch else '')+uuid.uuid4().hex
                pending_request=json.dumps({'admission_pending':True,'resources':resources}) if requirements is not None else row['request']
                db.execute("UPDATE submissions SET state='preparing',host=?,token=?,updated=?,request=? WHERE namespace=? AND id=?",(host,token,time.time(),pending_request,self.namespace,row['id']))
                return {**dict(row),'host':host,'token':token,'state':'preparing','admission_resources':resources}
        return None

    def update(self,ident,token,**fields):
        if not fields or set(fields)-{'state','host','task_id','request','result','error'}:raise ValueError('Invalid queue checkpoint')
        with self.connect() as db:
            self.begin(db)
            revoked=",token=NULL" if fields.get('state')=='queued' else ''
            cursor=db.execute('UPDATE submissions SET '+','.join(k+'=?' for k in fields)+revoked+',updated=? WHERE namespace=? AND id=? AND token=?',(*fields.values(),time.time(),self.namespace,ident,token))
            if cursor.rowcount!=1:raise RuntimeError('Stale or foreign queue claim')

    def recover(self,host):
        with self.connect() as db:return [dict(r) for r in db.execute("SELECT * FROM submissions WHERE namespace=? AND host=? AND state='accepted' ORDER BY created,id",(self.namespace,host))]

    def adopt(self,ident,host):
        """Attach an existing execution under the adapter's exclusive recovery lock."""
        with self.connect() as db:
            self.begin(db)
            row=db.execute('SELECT * FROM submissions WHERE namespace=? AND id=?',(self.namespace,ident)).fetchone()
            if row is None:raise KeyError(ident)
            if row['state']=='accepted':
                if row['host']!=host:raise RuntimeError('Existing execution belongs to another worker')
                if self.epoch and not str(row['token']).startswith(self.epoch+':'):
                    token=self.epoch+':'+uuid.uuid4().hex
                    db.execute('UPDATE submissions SET token=?,updated=? WHERE namespace=? AND id=?',(token,time.time(),self.namespace,ident))
                    return {**dict(row),'token':token}
                return dict(row)
            if row['state']=='completed':return dict(row)
            token=(self.epoch+':' if self.epoch else '')+uuid.uuid4().hex
            db.execute("UPDATE submissions SET state='accepted',host=?,token=?,updated=? WHERE namespace=? AND id=?",(host,token,time.time(),self.namespace,ident))
            return {**dict(row),'state':'accepted','host':host,'token':token}

    def recover_preparations(self, exclude=()):
        # Only under the adapter's exclusive controller lock; never reset accepted work.
        with self.connect() as db:
            self.begin(db)
            excluded=list(exclude)
            suffix=(' AND id NOT IN ('+','.join('?' for _ in excluded)+')') if excluded else ''
            db.execute("UPDATE submissions SET state='queued',host=NULL,token=NULL,updated=? WHERE namespace=? AND state='preparing'"+suffix,(time.time(),self.namespace,*excluded))

    def snapshot(self):
        with self.connect() as db:return dict(db.execute('SELECT state,count(*) FROM submissions WHERE namespace=? GROUP BY state',(self.namespace,)))


    def activate_epoch(self):
        """Fence fresh claims, preserving every in-flight token and its owner.

        The trigger also fences legacy controller binaries that predate epochs.
        It never changes an accepted job, container, resource lease or receipt.
        """
        if self.epoch is None:raise ValueError('A controller epoch is required')
        with self.connect() as db:
            self.begin(db)
            db.execute('CREATE TABLE IF NOT EXISTS submission_controller_epochs(namespace TEXT PRIMARY KEY, epoch TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS submission_handoff_claims(namespace TEXT, id TEXT, token TEXT, owner_epoch TEXT, PRIMARY KEY(namespace,id,token,owner_epoch))')
            db.execute("INSERT OR IGNORE INTO submission_handoff_claims SELECT namespace,id,token,? FROM submissions WHERE namespace=? AND state IN ('preparing','accepted') AND token IS NOT NULL",(self.epoch,self.namespace))
            db.execute('INSERT INTO submission_controller_epochs VALUES(?,?) ON CONFLICT(namespace) DO UPDATE SET epoch=excluded.epoch',(self.namespace,self.epoch))
            db.execute('DROP TRIGGER IF EXISTS submission_controller_claim_fence')
            db.execute("""CREATE TRIGGER submission_controller_claim_fence
                BEFORE UPDATE OF state ON submissions
                WHEN OLD.state='queued' AND NEW.state IN ('preparing','accepted')
                AND EXISTS(SELECT 1 FROM submission_controller_epochs e WHERE e.namespace=NEW.namespace
                    AND substr(COALESCE(NEW.token,''),1,length(e.epoch)+1)!=e.epoch||':')
                BEGIN SELECT RAISE(ABORT,'Controller superseded; existing jobs remain owned'); END""")
        return self.handoff_claims()

    def handoff_claims(self, *, active_only=False):
        """Legacy claim IDs that the successor must leave with their owners."""
        with self.connect() as db:
            if not db.execute("SELECT 1 FROM sqlite_master WHERE name='submission_handoff_claims'").fetchone():return []
            query="""SELECT s.* FROM submissions s JOIN submission_handoff_claims h
                ON s.namespace=h.namespace AND s.id=h.id AND s.token=h.token
                WHERE s.namespace=? AND h.owner_epoch=? AND substr(s.token,1,13)!=?"""
            if active_only:query+=" AND s.state IN ('preparing','accepted')"
            return [dict(r) for r in db.execute(query,(self.namespace,self.epoch,(self.epoch or '')+':'))]
