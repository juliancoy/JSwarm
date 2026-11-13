"""Rebuild bloated event history without deleting current task state.

Run with legacy database writers stopped. Updated EventStore writers coordinate
through the adjacent lock file. The original is replaced only after integrity
checks and exact comparison of every non-history table.
"""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import time

from task_events import encode, HISTORY_BYTES, HISTORY_DAYS


def table_digest(db, name):
    digest=hashlib.sha256();count=0
    for row in db.execute('SELECT * FROM "'+name+'" ORDER BY rowid'):
        digest.update(encode(row).encode());digest.update(b'\n');count+=1
    return count,digest.hexdigest()


def rebuild(path, max_bytes=HISTORY_BYTES, now=None):
    path=Path(path).resolve();temporary=path.with_name(path.name+'.compact')
    if max_bytes<0:raise ValueError('History budget must be nonnegative')
    cutoff=(time.time() if now is None else now)-HISTORY_DAYS*86400
    with path.with_suffix(path.suffix+'.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        if temporary.exists():raise FileExistsError(temporary)
        source=sqlite3.connect('file:'+str(path)+'?mode=rw',uri=True,timeout=30)
        target=None
        try:
            checkpoint=source.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()
            if checkpoint[0]:raise RuntimeError('Database has active legacy writers/readers')
            source.execute('BEGIN IMMEDIATE')
            before=path.stat().st_size
            schema=source.execute("SELECT type,name,sql FROM sqlite_master WHERE sql IS NOT NULL ORDER BY rowid").fetchall()
            target=sqlite3.connect(temporary)
            temporary.chmod(0o600)
            target.execute('PRAGMA auto_vacuum=INCREMENTAL')
            target.execute('PRAGMA journal_mode=DELETE')
            target.execute('PRAGMA synchronous=FULL')
            tables=[name for kind,name,sql in schema if kind=='table' and name not in ('events','sqlite_sequence')]
            for kind,name,sql in schema:
                if kind=='table' and name!='sqlite_sequence':target.execute(sql)
            expected={}
            for name in tables:
                expected[name]=table_digest(source,name)
                columns=len(source.execute('PRAGMA table_info("'+name+'")').fetchall())
                target.executemany('INSERT INTO "'+name+'" VALUES('+','.join('?'*columns)+')',
                                   source.execute('SELECT * FROM "'+name+'" ORDER BY rowid'))
            total=source.execute('SELECT count(*) FROM events').fetchone()[0]
            sequence=source.execute("SELECT seq FROM sqlite_sequence WHERE name='events'").fetchone()
            insert='INSERT INTO events VALUES(?,?,?,?,?,?,?,?)'
            kept=0;used=0
            # Replace each run's latest redundant full snapshot with a compact
            # checkpoint. Complete current JSON has already been copied to runs.
            for run,category,revision,digest,payload in source.execute("SELECT id,category,revision,digest,payload FROM runs WHERE category!='worker-session' ORDER BY id"):
                row=source.execute("SELECT * FROM events WHERE run_id=? AND kind='snapshot' ORDER BY id DESC LIMIT 1",(run,)).fetchone()
                if not row or row[6]<cutoff:continue
                value=json.loads(payload)
                summary=encode({'category':category,'revision':revision,'state_sha256':digest,
                                'status':value.get('status'),'note':'Current full state retained in runs; redundant snapshots removed.'})
                size=len(summary.encode())
                if used+size>max_bytes:continue
                target.execute(insert,(*row[:4],'state-checkpoint',*row[5:7],summary))
                kept+=1;used+=size
            # True worker/log/receipt events have priority over old snapshots.
            rows=source.execute("SELECT * FROM events WHERE kind NOT IN ('snapshot','annotation-snapshot') AND recorded_at>=? ORDER BY id DESC",(cutoff,))
            for row in rows:
                size=len(row[7].encode())
                if used+size>max_bytes:break
                target.execute(insert,row);kept+=1;used+=size
            for kind,name,sql in schema:
                if kind not in ('table',):target.execute(sql)
            if sequence:
                target.execute("DELETE FROM sqlite_sequence WHERE name='events'")
                target.execute("INSERT INTO sqlite_sequence VALUES('events',?)",sequence)
            target.execute('PRAGMA user_version='+str(source.execute('PRAGMA user_version').fetchone()[0]))
            target.commit()
            if target.execute('PRAGMA integrity_check').fetchall()!=[('ok',)]:
                raise RuntimeError('Rebuilt database integrity check failed')
            actual={name:table_digest(target,name) for name in tables}
            if expected!=actual:raise RuntimeError('Current state differs after rebuild')
            after=temporary.stat().st_size
            target.close();target=None
            with temporary.open('rb') as ready:os.fsync(ready.fileno())
            # No WAL pages remain; cooperative clients are held at the lock.
            os.replace(temporary,path)
            folder=os.open(path.parent,os.O_RDONLY|os.O_DIRECTORY)
            try:os.fsync(folder)
            finally:os.close(folder)
            return {'before_bytes':before,'after_bytes':after,'events_before':total,
                    'events_kept':kept,'payload_bytes':used,'preserved_tables':expected}
        finally:
            if target is not None:target.close()
            source.close()
            temporary.unlink(missing_ok=True)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('database',type=Path)
    parser.add_argument('--history-mib',type=int,default=256)
    args=parser.parse_args()
    print(json.dumps(rebuild(args.database,args.history_mib*1024**2),indent=2))
