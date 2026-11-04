"""Persistent execution samples, independent of dashboard/log retention."""
from contextlib import closing
from datetime import datetime, timezone, timedelta
import json
import math
from pathlib import Path
import sqlite3
import statistics

RETENTION_DAYS = 180


def number(value):
    return value if type(value) in (int, float) and math.isfinite(value) and value >= 0 else None


def seconds(start, end):
    try:return max(0, (datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds())
    except (TypeError, ValueError):return None


def connect(folder):
    db=sqlite3.connect(Path(folder)/'runtime-stats.sqlite3',timeout=10)
    db.row_factory=sqlite3.Row
    db.execute('''CREATE TABLE IF NOT EXISTS samples (
        run_id TEXT, host TEXT, task TEXT, file TEXT, status TEXT, finished TEXT,
        total REAL, test REAL, prepare REAL, dependency_wait REAL, timing_source TEXT,
        machine TEXT, source_sha256 TEXT, input_fingerprint TEXT,
        PRIMARY KEY(run_id,host,task))''')
    db.execute('CREATE INDEX IF NOT EXISTS sample_finished ON samples(finished)')
    return db


def record(folder, run):
    if not run.get('finished'):return
    with closing(connect(folder)) as db, db:
        for host in run.get('hosts',[]):
            for task, job in host.get('jobs',{}).items():
                phases=job.get('phase_seconds',{})
                # Reused results retain their original duration in run history;
                # never treat those as new runtime measurements.
                measured=job.get('status')!='reused' and not job.get('not_executed')
                db.execute('INSERT OR REPLACE INTO samples VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)',(
                    run['id'],host['host'],task,job.get('file'),job.get('status'),run['finished'],
                    number(job.get('duration_seconds')) if measured else None,
                    number(phases.get('test')) if measured else None,
                    number(phases.get('prepare')) if measured else None,
                    number(phases.get('waiting_for_dependencies')) if measured else None,
                    job.get('timing_source','coordinator_estimate'),host.get('session',{}).get('machine'),
                    run.get('source_sha256'),job.get('input_fingerprint')))
        cutoff=(datetime.now(timezone.utc)-timedelta(days=RETENTION_DAYS)).isoformat()
        db.execute('DELETE FROM samples WHERE finished < ?', (cutoff,))


def summary(values):
    values=sorted(v for v in values if v is not None)
    if not values:return {'samples':0}
    return {'samples':len(values),'min':round(values[0],3),'median':round(statistics.median(values),3),
            'p95':round(values[math.ceil(len(values)*.95)-1],3),'max':round(values[-1],3)}


def report(folder, host=None, task=None):
    where=[];args=[]
    for key,value in [('host',host),('task',task)]:
        if value:where.append(key+'=?');args.append(value)
    with closing(connect(folder)) as db:
        rows=db.execute('SELECT * FROM samples'+(' WHERE '+' AND '.join(where) if where else '')+' ORDER BY finished DESC LIMIT 20001',args).fetchall()
    truncated=len(rows)>20000
    groups={}
    for raw in rows[:20000]:
        row=dict(raw);key=(row['host'],row['machine'],row['task'])
        if key not in groups and len(groups)>=200:truncated=True;continue
        group=groups.setdefault(key,[])
        if len(group)<100:group.append(row)
    result=[]
    for (hostname,machine,ident),samples in groups.items():
        fresh=[r for r in samples if r['status']=='passed' and r['total'] is not None]
        result.append({'host':hostname,'machine':machine,'task':ident,'file':samples[0]['file'],
                       'observations':len(samples),'reused':sum(r['status']=='reused' for r in samples),
                       'failed':sum(r['status'] in ('failed','timed_out','unavailable','interrupted') for r in samples),
                       'total_seconds':summary([r['total'] for r in fresh]),
                       'test_seconds':summary([r['test'] for r in fresh]),
                       'prepare_seconds':summary([r['prepare'] for r in fresh]),
                       'dependency_wait_seconds':summary([r['dependency_wait'] for r in fresh]),
                       'latest':samples[0]})
    return {'retention_days':RETENTION_DAYS,'sample_limit_per_task_machine':100,'group_limit':200,
            'truncated':truncated,'percentile_method':'nearest-rank',
            'policy':'Fresh passing executions only in percentiles; grouped by task and machine identity. Sources/images may differ; latest sample includes provenance. Older timings are coordinator estimates.',
            'statistics':result}
