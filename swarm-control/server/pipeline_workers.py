"""Bounded asynchronous observations of cache workers outside the task queue."""
from concurrent.futures import ThreadPoolExecutor
import json
import threading
import time
from agent_client import AgentClient, CONFIG

POOL = ThreadPoolExecutor(max_workers=5, thread_name_prefix='pipeline-workers')
LOCK = threading.Lock()
CACHE = {}


def project(host, status, observed):
    if status.get('node') != host:
        raise ValueError('JSwarm node identity differs')
    items = []
    for worker in status.get('workers', [])[:4096]:
        if worker.get('group') != 'shared-cache' or worker.get('active') is not True:
            continue
        ident = worker.get('id')
        if not isinstance(ident, str) or not ident:
            continue
        resources = {k: worker[k] for k in ('cpus', 'memory_mb')
                     if type(worker.get(k)) in (int, float) and worker[k] > 0}
        items.append({'id': 'node-cache/' + host + '/' + ident,
                      'job_id': worker.get('job'), 'name': 'Shared cache · ' + str(worker.get('container') or ident),
                      'parent': 'Node infrastructure', 'kind': 'shared-cache', 'run_type': 'cache',
                      'status': 'running', 'queue_state': 'node-observed', 'worker': host,
                      'resources': resources, 'worker_resources': resources,
                      'resident_mb': worker.get('resident_mb'), 'container': worker.get('container'),
                      'created': worker.get('created') or observed, 'updated': observed,
                      'detail': 'Persistent shared-cache worker observed through the authenticated node API. Reserved RAM is separate from measured resident RAM.'})
    return items


def collect(host, address):
    client = None
    try:
        client = AgentClient({'host': host, 'agent_endpoint': 'http://' + address + ':8790'})
        items = project(host, client.call('status', timeout=4), time.time())
        with LOCK:
            CACHE[host].update(items=items, observed=time.monotonic(), error=None)
    except Exception:
        with LOCK:
            CACHE[host]['error'] = 'Shared-cache observations unavailable: ' + host
    finally:
        if client is not None:
            client.close()
        with LOCK:
            CACHE[host].update(busy=False, checked=time.monotonic())


def snapshot():
    """Serve cached data immediately; never poll the fleet on an HTTP thread."""
    try:
        peers = json.loads(CONFIG.read_text())['peers']
    except (OSError, ValueError, KeyError):
        return {'items': [], 'errors': ['Shared-cache enrollment unavailable']}
    items, errors = [], []
    with LOCK:
        for host, address in list(peers.items())[:24]:
            row = CACHE.setdefault(host, {'items': [], 'checked': 0, 'observed': 0, 'busy': False})
            if not row['busy'] and time.monotonic() - row['checked'] >= 15:
                row['busy'] = True
                POOL.submit(collect, host, address)
            if time.monotonic() - row['observed'] <= 60:
                items.extend(row['items'])
            if row.get('error'):
                errors.append(row['error'])
    return {'items': items, 'errors': errors}
