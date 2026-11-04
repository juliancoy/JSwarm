"""Owner-only workload history projected from existing coordinator records."""
import math
import json
import os
import re
from pathlib import Path
from urllib.request import Request,urlopen
from urllib.error import HTTPError
from status import read_coordinator

SUITE_NAMES={'web':'Web tests','visual':'Web layout tests','org-api':'Organization API tests','chat':'Chat tests','identity':'Identity tests','identity-python':'Identity Python tests','monitor':'Coordinator tests','map':'Network map tests','workspace':'Workspace tests','swarm-web':'Swarm Control browser tests','mobile':'Android browser tests','p2p':'Peer connection tests','gpu':'GPU tests','usajobs':'USAJOBS browser tests'}
ACTIVE={'queued','preparing','running','stopping'}


def requests():
    from master_api import TOKEN_FILE
    token=Path(os.getenv('MAP_MASTER_TOKEN_FILE',str(TOKEN_FILE))).read_text().strip()
    request=Request('http://127.0.0.1:8089/api/v1/master/test-requests',headers={'Authorization':'Bearer '+token})
    with urlopen(request,timeout=4) as response:raw=response.read(4*1024*1024+1)
    if len(raw)>4*1024*1024:raise ValueError('History exceeded the size limit')
    return json.loads(raw)['requests']


def machine_contribution(host):
    jobs=list((host.get('jobs') or {}).values())
    fresh=[job for job in jobs if job.get('status')!='reused' and not job.get('reused') and not job.get('not_executed')]
    def number(value):return value if type(value) in (int,float) and math.isfinite(value) and value>=0 else None
    measured=[number(job.get('cpu_seconds')) for job in fresh]
    estimates=[]
    for job in fresh:
        duration=number((job.get('phase_seconds') or {}).get('test'))
        if duration is None:duration=number(job.get('duration_seconds'))
        cpus=number(job.get('cpus'))
        if duration is not None and cpus is not None and cpus>0:estimates.append(duration*cpus)
    values=[value for value in measured if value is not None]
    return {'completed_tasks':sum(job.get('status')=='passed' for job in fresh),
            'executed_tasks':sum(job.get('status') in ('passed','failed','timed_out','interrupted','stopped') for job in fresh),
            'reused_tasks':sum(job.get('status')=='reused' or bool(job.get('reused')) for job in jobs),
            'cpu_seconds':sum(values) if values else None,'cpu_measured_tasks':len(values),
            'estimated_cpu_slot_seconds':sum(estimates) if estimates else None,'cpu_estimated_tasks':len(estimates)}


def project(record,run):
    selection=record.get('selection') or run.get('selection') or {}
    genome=record.get('kind')=='research' or run.get('kind')=='research' or 'research' in selection
    if genome:
        profile=selection.get('research') or run.get('profile')
        title='Genome analysis'+(' — '+str(profile)[:80] if profile else '')
    else:
        suites=selection.get('suites') or []
        labels=[SUITE_NAMES.get(suite,str(suite)[:80]) for suite in suites[:8]]
        paths=selection.get('paths') or []
        web=bool(suites) and all(suite in ('web','visual','swarm-web','mobile','usajobs') for suite in suites)
        web=web or bool(paths) and all(path.startswith('OrgPortal/web/') for path in paths)
        title=('Web tests' if web else 'Distributed tests')
        details=[name for name in labels if name!=title]
        details += [Path(path).name[:100] for path in paths[:4]]
        if details:title+=' — '+', '.join(details)
        elif not suites and not paths:title+=' — Full suite'
    hosts=run.get('hosts') or []
    jobs=[job for host in hosts for job in (host.get('jobs') or {}).values()]
    progress=run.get('progress')
    if not isinstance(progress,dict):
        progress={'total':len(jobs) if hosts else run.get('suites'),
                  'completed':sum(job.get('status') in ('passed','reused') for job in jobs) if hosts else (run.get('passed',0) or 0)+(run.get('reused',0) or 0),
                  'failed':sum(job.get('status') in ('failed','timed_out','unavailable','interrupted') for job in jobs) if hosts else run.get('failed',0)}
    return {'id':record.get('id') or run.get('id'),'run_id':record.get('run_id') or run.get('id'),
            'kind':'genome' if genome else 'tests','title':title,'status':run.get('status') or record.get('status','unknown'),
            'submitted_at':record.get('submitted_at') or run.get('started'),
            'started_at':run.get('started') or record.get('started_at'),'finished_at':run.get('finished') or record.get('finished_at'),
            'progress':{key:progress.get(key) for key in ('total','completed','failed')},
            'machines':[{'name':host.get('host'),'status':host.get('status'),**machine_contribution(host),
                         'tasks':list(dict.fromkeys(str(job.get('name') or key)[:200] for key,job in (host.get('jobs') or {}).items()))[:20]}
                        for host in hosts[:64]]}


def snapshot():
    records=sorted(requests(),key=lambda row:(row.get('status') in ACTIVE,row.get('submitted_at') or ''),reverse=True)[:30]
    feed=read_coordinator('/api/distributed-tests')
    runs={row['id']:row for row in feed.get('history',[]) if row.get('id')}
    if (feed.get('run') or {}).get('id'):runs[feed['run']['id']]=feed['run']
    rows=[];seen=set()
    for record in records:
        ident=record.get('run_id');run=runs.get(ident,{})
        if ident and re.fullmatch(r'[0-9TZ-]+-[a-f0-9]{8}',ident) and (record.get('status') in ACTIVE or 'hosts' not in run):
            try:run=read_coordinator('/api/distributed-tests?id='+ident).get('run') or run
            except HTTPError as error:
                if error.code!=400 and error.code!=404:raise
        rows.append(project(record,run))
        if ident:seen.add(ident)
    for ident,run in runs.items():
        if ident not in seen:
            if 'hosts' not in run and re.fullmatch(r'[0-9TZ-]+-[a-f0-9]{8}',ident):
                try:run=read_coordinator('/api/distributed-tests?id='+ident).get('run') or run
                except HTTPError as error:
                    if error.code not in (400,404):raise
            rows.append(project({},run))
    rows.sort(key=lambda row:(row['status'] in ACTIVE,row.get('submitted_at') or ''),reverse=True)
    return {'jobs':rows[:30],'limit':30}
