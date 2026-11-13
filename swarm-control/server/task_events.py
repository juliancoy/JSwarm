"""Transactional compute journal with current state and bounded event history."""
from contextlib import contextmanager
import fcntl
import hashlib
import json
import sqlite3
import time
from pathlib import Path
from paths import STATE


def encode(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


OBSERVATION_TIMES = frozenset(('updated', 'sampled_at', 'observed_at', 'checked_at'))
HISTORY_BYTES = 256 * 1024**2
HISTORY_DAYS = 30


def meaningful(value):
    """Polling timestamps do not constitute a task state change."""
    if isinstance(value, dict):
        return {k: meaningful(v) for k, v in value.items() if k not in OBSERVATION_TIMES}
    if isinstance(value, list):
        return [meaningful(v) for v in value]
    return value


def change_summary(before, after):
    """Bounded audit summary; the complete current state lives in runs."""
    changes=[]
    truncated=False
    def brief(value):
        text=encode(value)
        if len(text.encode())<=1024:return value
        return {'summary_sha256':hashlib.sha256(text.encode()).hexdigest(),
                'bytes':len(text.encode()),'preview':text[:256]}
    def visit(old,new,path):
        nonlocal truncated
        if old==new:return
        if len(changes)>=64:
            truncated=True;return
        if isinstance(old,dict) and isinstance(new,dict):
            for key in sorted(old.keys()|new.keys()):
                if key not in new:changes.append({'path':path+[key],'deleted':True})
                elif key not in old:changes.append({'path':path+[key],'value':brief(new[key])})
                else:visit(old[key],new[key],path+[key])
                if len(changes)>=64:
                    truncated=True;break
        elif isinstance(old,list) and isinstance(new,list) and len(old)==len(new):
            for index,(a,b) in enumerate(zip(old,new)):
                visit(a,b,path+[index])
                if len(changes)>=64:
                    truncated=True;break
        else:changes.append({'path':path,'value':brief(new)})
    visit(meaningful(before),meaningful(after),[])
    return {'changes':changes,'truncated':truncated}


class EventStore:
    def __init__(self, path=None):
        self.path = Path(path) if path else STATE/'compute-events.sqlite3'
        new_database=not self.path.exists()
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.connect() as db:
            if new_database:db.execute('PRAGMA auto_vacuum=INCREMENTAL')
            db.executescript('''
                CREATE TABLE IF NOT EXISTS events (
                  id INTEGER PRIMARY KEY AUTOINCREMENT, event_key TEXT UNIQUE,
                  run_id TEXT NOT NULL, source TEXT NOT NULL, kind TEXT NOT NULL,
                  occurred_at TEXT, recorded_at REAL NOT NULL, payload TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS events_run ON events(run_id,id);
                CREATE TABLE IF NOT EXISTS runs (
                  id TEXT PRIMARY KEY, category TEXT NOT NULL, revision INTEGER NOT NULL,
                  updated REAL NOT NULL, digest TEXT NOT NULL, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS work_items (
                  run_id TEXT NOT NULL, item_id TEXT NOT NULL, parent_id TEXT,
                  name TEXT NOT NULL, status TEXT, worker TEXT, payload TEXT NOT NULL,
                  PRIMARY KEY(run_id,item_id));
                CREATE INDEX IF NOT EXISTS work_item_status ON work_items(status,run_id);
                CREATE TABLE IF NOT EXISTS worker_jobs (node TEXT NOT NULL, job TEXT NOT NULL, run_id TEXT NOT NULL, PRIMARY KEY(node,job));
                CREATE TABLE IF NOT EXISTS cursors (source TEXT PRIMARY KEY, offset INTEGER NOT NULL);
                CREATE TRIGGER IF NOT EXISTS immutable_events_update BEFORE UPDATE ON events
                  BEGIN SELECT RAISE(ABORT,'Event history is append-only'); END;
                CREATE TRIGGER IF NOT EXISTS immutable_events_delete BEFORE DELETE ON events
                  BEGIN SELECT RAISE(ABORT,'Event history is append-only'); END;
            ''')
            if db.execute('PRAGMA user_version').fetchone()[0] < 2:
                db.execute('BEGIN IMMEDIATE')
                for row in db.execute('SELECT payload FROM runs').fetchall():
                    self._project_items(db,json.loads(row[0]))
                db.execute('PRAGMA user_version=2')
        self.path.chmod(0o600)

    @contextmanager
    def connect(self):
        # Maintenance replaces the database only while all connections are closed.
        with self.path.with_suffix(self.path.suffix+'.lock').open('a') as lock:
            fcntl.flock(lock,fcntl.LOCK_SH)
            db=sqlite3.connect(self.path, timeout=30)
            try:
                db.execute('PRAGMA journal_mode=WAL')
                db.execute('PRAGMA synchronous=FULL')
                db.execute('PRAGMA foreign_keys=ON')
                with db: yield db
            finally: db.close()

    @staticmethod
    def _append(db, run, source, kind, value, key=None):
        db.execute('INSERT OR IGNORE INTO events(event_key,run_id,source,kind,occurred_at,recorded_at,payload) VALUES(?,?,?,?,?,?,?)',
                   (key, run, source, kind, str(value.get('time',value.get('timestamp',''))) or None, time.time(), encode(value)))

    def append(self, run, source, kind, value, key=None):
        if key is None and '_worker_sequence' in value:
            key = ':'.join(map(str, ('worker', source, value.get('_worker_job'), value['_worker_sequence'])))
        with self.connect() as db:
            self._append(db,run,source,kind,value,key)

    def append_batch(self, records):
        with self.connect() as db:
            for run,source,kind,value,key in records:
                if key is None and '_worker_sequence' in value:
                    key = ':'.join(map(str, ('worker', source, value.get('_worker_job'), value['_worker_sequence'])))
                self._append(db,run,source,kind,value,key)

    def observe(self, run, category='task', source='executor'):
        """A changed snapshot event and its projection commit together."""
        payload=encode(run);digest=hashlib.sha256(encode(meaningful(run)).encode()).hexdigest()
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            previous=db.execute('SELECT digest,revision,category,payload FROM runs WHERE id=?',(run['id'],)).fetchone()
            if previous and previous[2]=='application-tests' and category!='application-tests':
                # Presentation metadata must never replace pinned inputs or job
                # receipts used by the retry scheduler. Retain it as an event.
                self._append(db,run['id'],source,'annotation-change',{'category':category,**change_summary(json.loads(previous[3]),run)},
                             'annotation:'+run['id']+':'+category+':'+digest)
                return
            if previous and previous[2]==category and meaningful(json.loads(previous[3]))==meaningful(run):
                db.execute('UPDATE runs SET payload=?,digest=?,updated=? WHERE id=?',
                           (payload,digest,time.time(),run['id']))
                self._project_items(db,run)
                return
            revision=previous[1]+1 if previous else 1
            self._append(db,run['id'],source,'state-change',
                         {'revision':revision,'category':category,'status':run.get('status'),
                          'state_sha256':digest,**change_summary(json.loads(previous[3]) if previous else {},run)})
            db.execute('INSERT INTO runs VALUES(?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET category=excluded.category,revision=excluded.revision,updated=excluded.updated,digest=excluded.digest,payload=excluded.payload',
                       (run['id'],category,revision,time.time(),digest,payload))
            self._project_items(db,run)

    def retain_history(self, now=None, max_bytes=HISTORY_BYTES, days=HISTORY_DAYS):
        """Keep the newest events within age and payload budgets; retain all current state.

        Ordinary writers cannot mutate history. Only this maintenance transaction
        temporarily removes the delete guard. Event IDs and ingest cursors survive.
        """
        if max_bytes<0 or days<0:raise ValueError('Retention limits must be nonnegative')
        cutoff=(time.time() if now is None else now)-days*86400
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            used=0;keep_from=None
            for ident,recorded,size in db.execute('SELECT id,recorded_at,length(CAST(payload AS BLOB)) FROM events ORDER BY id DESC'):
                used+=size
                if recorded<cutoff or used>max_bytes:break
                keep_from=ident
            db.execute('DROP TRIGGER IF EXISTS immutable_events_delete')
            if keep_from is None:
                removed=db.execute('DELETE FROM events').rowcount
            else:
                removed=db.execute('DELETE FROM events WHERE id<?',(keep_from,)).rowcount
            db.execute("""CREATE TRIGGER immutable_events_delete BEFORE DELETE ON events
                          BEGIN SELECT RAISE(ABORT,'Event history is append-only'); END""")
            # New/rebuilt databases use incremental vacuum to return freed pages.
            db.execute('PRAGMA incremental_vacuum(1024)')
        return removed

    @staticmethod
    def _project_items(db, run):
        rows=[]
        def add(ident, item, parent=None, worker=None):
            rows.append((run['id'],ident,parent,item.get('name',ident),item.get('status'),item.get('worker',item.get('host',worker)),encode(item)))
            for index,child in enumerate(item.get('children',[])):
                add(ident+'/'+str(child.get('id',index)),child,ident)
        for index,host in enumerate(run.get('hosts',[])):
            node=host.get('host') or 'unassigned-'+str(index)
            if host.get('agent_job_id'):
                db.execute('INSERT INTO worker_jobs VALUES(?,?,?) ON CONFLICT(node,job) DO UPDATE SET run_id=excluded.run_id',(node,host['agent_job_id'],run['id']))
            for ident,item in host.get('jobs',{}).items():
                add(node+'/'+ident,item,worker=node)
        for ident,item in run.get('jobs',{}).items():add(ident,item)
        for index,item in enumerate(run.get('children',[])):add(str(item.get('id',index)),item)
        db.executemany('INSERT INTO work_items VALUES(?,?,?,?,?,?,?) ON CONFLICT(run_id,item_id) DO UPDATE SET parent_id=excluded.parent_id,name=excluded.name,status=excluded.status,worker=excluded.worker,payload=excluded.payload WHERE work_items.payload!=excluded.payload',rows)

    def ingest_lines(self, source, run, lines):
        """Replay numbered JSONL events and advance cursor in the same transaction."""
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            offset=db.execute('SELECT offset FROM cursors WHERE source=?',(source,)).fetchone()
            offset=offset[0] if offset else 0
            for index,value in lines:
                if index<=offset:continue
                if index!=offset+1:raise ValueError('Event history has a gap')
                self._append(db,run,source,value.get('action',value.get('kind','event')),value,value.get('event_id') or source+':'+str(index))
                offset=index
            db.execute('INSERT INTO cursors VALUES(?,?) ON CONFLICT(source) DO UPDATE SET offset=excluded.offset',(source,offset))

    def ingest_worker(self, source, node, rows):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            previous=db.execute('SELECT offset FROM cursors WHERE source=?',(source,)).fetchone()
            offset=previous[0] if previous else 0
            for rowid,job,sequence,payload,recorded_at in rows:
                if rowid<=offset:continue
                if rowid!=offset+1:raise ValueError('Worker event journal has a gap')
                mapped=db.execute('SELECT run_id FROM worker_jobs WHERE node=? AND job=?',(node,job)).fetchone()
                run=mapped[0] if mapped else 'worker-session-'+job
                if not mapped:
                    summary=encode({'id':run,'name':'Worker session '+job,'status':'observed','worker':node})
                    db.execute('INSERT OR IGNORE INTO runs VALUES(?,?,?,?,?,?)',(run,'worker-session',1,time.time(),hashlib.sha256(summary.encode()).hexdigest(),summary))
                value=json.loads(payload)
                value.update(_worker_sequence=sequence,_worker_job=job,worker_recorded_at=recorded_at)
                self._append(db,run,node,value.get('kind','event'),value,':'.join(map(str,('worker',node,job,sequence))))
                offset=rowid
            db.execute('INSERT INTO cursors VALUES(?,?) ON CONFLICT(source) DO UPDATE SET offset=excluded.offset',(source,offset))

    def cursor(self, source):
        with self.connect() as db:
            row=db.execute('SELECT offset FROM cursors WHERE source=?',(source,)).fetchone()
            return row[0] if row else 0

    def list_runs(self, category=None):
        with self.connect() as db:
            query='SELECT payload FROM runs'+(' WHERE category=?' if category else '')+' ORDER BY updated DESC'
            return [json.loads(row[0]) for row in db.execute(query,(category,) if category else ())]

    def history(self, run=None, after=0, limit=100):
        limit=max(1,min(int(limit),500));after=max(0,int(after))
        with self.connect() as db:
            where='id>?'+(' AND run_id=?' if run else '')
            args=(after,run,limit) if run else (after,limit)
            rows=db.execute('SELECT id,run_id,source,kind,occurred_at,recorded_at,payload FROM events WHERE '+where+' ORDER BY id LIMIT ?',args).fetchall()
            total=db.execute('SELECT count(*) FROM events'+(' WHERE run_id=?' if run else ''),(run,) if run else ()).fetchone()[0]
            run_rows=db.execute("SELECT id,category,coalesce(json_extract(payload,'$.name'),json_extract(payload,'$.title'),id) FROM runs ORDER BY updated DESC").fetchall()
            runs=len(run_rows)
        return {'events':[dict(zip(('id','run_id','source','kind','occurred_at','recorded_at','payload'),(*r[:6],json.loads(r[6])))) for r in rows],
                'cursor':rows[-1][0] if rows else after,'total_events':total,'total_runs':runs,
                'runs':[{'id':r[0],'category':r[1],'name':r[2]} for r in run_rows]}
