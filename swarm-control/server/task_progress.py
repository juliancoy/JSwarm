"""Read-only global task projection; execution remains owned by its workers."""
import math
import json
import time
from pathlib import Path

LABELS = {'bam-read-qc': 'Read quality statistics', 'fasta-summary': 'Assembly sequence summary', 'variant-summary': 'Published variant summary'}


def aggregate(children):
    leaves = []
    def visit(rows):
        for row in rows:
            if row.get('children'): visit(row['children'])
            else: leaves.append(row)
    visit(children)
    total = len(leaves)
    complete = sum(c['status'] in ('completed', 'passed', 'reused') for c in leaves)
    result = {'completed': complete, 'total': total,
            'percent': round(100 * complete / total, 1) if total else None,
            'basis': 'Verified completed work items; not an estimate of elapsed compute time'}
    measured=[c for c in leaves if type(c.get('total_units')) in (int,float) and math.isfinite(c['total_units']) and c['total_units']>0]
    units={c.get('unit','work units') for c in measured}
    if len(measured)==total and total and len(units)==1:
        denominator=sum(c['total_units'] for c in measured)
        numerator=sum(c['total_units'] if c['status'] in ('completed','passed','reused') else min(c['total_units'],max(0,c.get('completed_units') or 0)) for c in measured)
        result.update(work_percent=round(100*numerator/denominator,2),completed_units=numerator,total_units=denominator,unit=units.pop(),
                      lower_bound=any(c['status'] not in ('completed','passed','reused','queued','not scheduled') and c.get('completed_units') is None for c in measured))
    return result


def peer_task(manifest, state, receipts, now=None, measurements=None, failures=None):
    now = time.time() if now is None else now
    measurements=measurements or {}
    failures=failures or {}
    observations = {state.get('node'): state, **state.get('peers', {})}
    children = []
    for job in manifest['jobs']:
        ident = job['id']; spec = job['spec']; receipt = receipts.get(ident)
        running = [name for name, row in observations.items()
                   if now - row.get('time', 0) < 60 and row.get('current_job') == ident]
        stale = [name for name, row in observations.items()
                 if now - row.get('time', 0) >= 60 and row.get('current_job') == ident]
        conflict = any(ident in row.get('conflicts', []) for row in observations.values())
        status = 'conflict' if conflict else 'completed' if receipt else 'running' if running else 'unknown' if stale else 'failed' if ident in failures else 'queued'
        children.append({'id': ident, 'name': job.get('title') or LABELS.get(spec['kind'], spec['kind']),
                         'input': spec['path'], 'group': LABELS.get(spec['kind'], spec['kind']), 'status': status,
                         'worker': receipt['node'] if receipt else ', '.join(running or stale) or job['preferred_worker'],
                         'percent': 100 if status == 'completed' else 0 if status == 'queued' else None,
                         'detail': spec['scope'] + (f" · {failures[ident]} failed attempt(s); automatic retry pending." if status=='failed' else f" · Retrying after {failures[ident]} failed attempt(s)." if status=='running' and ident in failures else ''), 'elapsed_seconds': receipt.get('elapsed_seconds') if receipt else None})
    for child in children:
        retry_rows=[row.get('retry',{}).get(child['id']) for row in observations.values() if now-row.get('time',0)<90]
        retry_rows=[r for r in retry_rows if r]
        if retry_rows:
            retry=dict(max(retry_rows,key=lambda r:r.get('updated',0)));retry['attempts']=sum(r['attempts'] for r in retry_rows)
            if retry['attempts']>=3 and child['status'] not in ('completed','running'):retry.update(state='exhausted',next_due=None)
            child['retry']=retry
            if child['status'] not in ('completed','running') and retry['state'] in ('blocked','exhausted','backoff'):
                child.update(status='failed',percent=None)
            child['detail']+=' · Attempt '+str(retry['attempts'])+'/3 · '+retry['state']+((': '+retry['reason']) if retry.get('reason') else '')
        spec=next(j['spec'] for j in manifest['jobs'] if j['id']==child['id'])
        child.update(total_units=spec.get('bytes'),unit='input bytes',completed_units=spec.get('bytes') if child['status']=='completed' else 0 if child['status']=='queued' else None)
        metric=measurements.get(child['id'],{})
        if child['status']=='running' and metric.get('run')==manifest['id'] and 0<=now-metric.get('sampled_at',0)<90 and metric.get('worker') in child['worker'].split(', '):
            child.update({k:metric[k] for k in ('completed_units','units_per_second','read_eta_seconds','basis','sampled_at') if k in metric})
            child['percent']=min(99.9,round(100*child['completed_units']/child['total_units'],1)) if child['total_units'] else None
    return {'id': manifest['id'], 'name': manifest.get('title') or manifest['id'],
            'delegated_runs':list(dict.fromkeys(j['run'] for j in manifest.get('delegated_jobs',[]))),
            'delegation_jobs':manifest.get('delegated_jobs',[]),
            'status': 'completed' if children and all(c['status'] == 'completed' for c in children) else 'active',
            'updated': state.get('time'), 'stale': now - state.get('time', 0) > 60,
            'progress': aggregate(children), 'children': children,
            'detail': 'Signed peer receipts; raw BAMs stay on their source node. This run does not align reads or call new variants.'}


def application_test_task(run):
    grouped = {key: [] for key in run.get('task_catalog', [])}
    for host in run.get('hosts', []):
        for name, job in host.get('jobs', {}).items():
            if name=='p2p' and run.get('selection') and 'p2p' not in run['selection'].get('suites',[]):continue
            grouped.setdefault(name, []).append((host['host'], job))
    children = []
    for name, attempts in grouped.items():
        selected = next(((h, j) for h, j in attempts if j.get('status') in ('passed', 'reused')), None)
        selected = selected or next(((h, j) for h, j in attempts if j.get('status') in ('running', 'preparing', 'transferring')), None)
        host, job = selected or (attempts[-1] if attempts else ('unassigned', {}))
        status = job.get('status', 'queued')
        children.append({'id': name, 'name': job.get('name') or name, 'group': job.get('suite') or name.split('--')[0], 'input': job.get('file'), 'worker': host, 'status': status,
                         'percent': 100 if status in ('passed', 'reused') else None,
                         'detail': job.get('reason', '')})
    if not children and run.get('status')=='preparing':
        hosts=run.get('hosts',[]);ready=sum(bool(h.get('session',{}).get('verified')) for h in hosts)
        waiting=[h['host'] for h in hosts if not h.get('session',{}).get('verified')]
        detail=f'{ready}/{len(hosts)} workers verified'+('; awaiting '+', '.join(waiting) if waiting else '')
        children=[{'id':'worker-preparation','name':'Prepare selected test workers','status':'preparing','detail':detail,'worker':'unassigned'}]
    detail=run.get('preparation') or ('Selected tests: '+', '.join(run['selection'].get('paths',[])+run['selection'].get('suites',[])) if run.get('selection') else '')
    return {'id': run.get('id'), 'kind':'application-tests', 'name': run.get('title') or 'Application tests — ' + str(run.get('id', 'current')),
            'status': run.get('status'), 'detail':detail,'progress': aggregate(children), 'children': children}


def current_tasks(tasks, registered):
    """A verified recovery replaces failed current work; original run history stays."""
    recovered=set()
    success={'completed','passed','reused','verified'}
    for task in registered:
        children=task.get('children',[])
        if (task.get('kind')=='application-tests' and task.get('status') in success and
                children and all(child.get('status') in success for child in children)):
            recovered.update(value for value in task.get('recovery_of',[]) if isinstance(value,str))
    return [task for task in tasks if not (task.get('id') in recovered and
            task.get('kind')=='application-tests' and task.get('status') in ('failed','stopped','partial','interrupted'))]


def snapshot():
    tasks = []; errors = []
    for root in sorted((Path.home()/'.local/state/venter-peer-qc').glob('venter-qc-*')):
        try:
            # Use the signed run's own verifier; do not execute a substituted worker.
            import hashlib, importlib.util
            from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
            import base64
            envelope = json.loads((root/'manifest.json').read_text())
            key = json.loads((Path.home()/'.config/lifetech-launcher/peers.json').read_text())['controller_public_key']
            value = envelope['value']
            canonical = json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()
            Ed25519PublicKey.from_public_bytes(bytes.fromhex(key)).verify(base64.b64decode(envelope['signature']), canonical)
            if hashlib.sha256((root/'worker.py').read_bytes()).hexdigest() != value['worker_sha256']:
                raise ValueError('Worker identity mismatch')
            module_spec = importlib.util.spec_from_file_location('task_peer_worker', root/'worker.py')
            module = importlib.util.module_from_spec(module_spec); module_spec.loader.exec_module(module)
            peer = module.Peer(root)
            receipts = {ident: row['value'] for ident, row in peer.receipts.items()}
            from paths import STATE
            measurements={}
            for path in (STATE/'input-progress').glob('*.json'):
                try:measurements[path.stem]=json.loads(path.read_text())
                except (ValueError,OSError):continue
            from task_events import EventStore
            with EventStore().connect() as db:
                failures={ident:count for ident,count in db.execute("SELECT json_extract(payload,'$.job'),count(*) FROM events WHERE run_id=? AND kind='job_deferred' AND json_extract(payload,'$.reason') LIKE 'QC command failed%' GROUP BY json_extract(payload,'$.job')",(value['id'],))}
            tasks.append(peer_task(value, json.loads((root/'state.json').read_text()), receipts,measurements=measurements,failures=failures))
        except Exception as error:
            errors.append(root.name + ': ' + type(error).__name__)
    try:
        from urllib.request import urlopen
        with urlopen('http://127.0.0.1:8089/api/distributed-tests', timeout=3) as response:
            data = json.load(response)
        if data.get('run'): tasks.append(application_test_task(data['run']))
    except Exception:
        errors.append('Application test status unavailable')
    from task_registry import registry_snapshot
    registered, planned, labels, registry_errors = registry_snapshot()
    tasks=current_tasks(tasks,registered)
    registered=current_tasks(registered,registered)
    from run_manager_projection import attach_delegated_runs
    tasks,registered=attach_delegated_runs(tasks,registered,aggregate)
    for task in tasks:
        if task['id'] in labels: task['name'] = labels[task['id']]
    from task_events import EventStore
    store=EventStore()
    for task in tasks:
        if task.get('kind') == 'application-tests': continue # Preserve the richer coordinator state in the database.
        stable={k:v for k,v in task.items() if k not in ('updated','stale')}
        store.observe(stable,'projection','verified-task-projection')
    from retry_jobs import RetryQueue
    retry_rows=RetryQueue(store).rows()
    return {'sampled_at': time.time(), 'tasks': sorted(tasks + registered,key=lambda t: -(t.get('priority',0) if type(t.get('priority',0)) is int else 0)),
            'errors': errors + registry_errors, 'planned': planned,'retry_jobs':retry_rows}
