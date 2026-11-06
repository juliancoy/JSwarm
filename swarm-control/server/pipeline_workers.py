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
    workers = [w for w in status.get('workers', [])[:4096]
               if w.get('group') == 'shared-cache' and w.get('active') is True]
    if not workers:return []
    resources = {k:sum(w.get(k,0) for w in workers if type(w.get(k)) in (int,float))
                 for k in ('cpus','memory_mb')}
    resident = [w.get('resident_mb') for w in workers]
    summary = status.get('cache_summary') or {}
    return [{'id':'node-cache/'+host, 'name':'Shared cache · '+host,
             'parent':'Node infrastructure', 'kind':'shared-cache', 'run_type':'cache',
             'status':'running', 'queue_state':'node-observed', 'worker':host,
             'resources':resources, 'worker_resources':resources,
             'resident_mb':sum(resident) if all(type(v) in (int,float) for v in resident) else None,
             'container':workers[0].get('container') if len(workers)==1 else None,
             'cache_workers':len(workers), **{k:summary[k] for k in ('objects','pinned_objects','budget_mb','used_mb') if k in summary},
             'created':min(w.get('created') or observed for w in workers), 'updated':observed,
             'detail':'Node shared RAM cache. Objects are pinned while loading, executing or transferring; idle managed objects are evictable. Reserved RAM is separate from measured resident RAM.'}]


def collect(host, address):
    client = None
    try:
        client = AgentClient({'host': host, 'agent_route':'auto','agent_endpoint': 'http://' + address + ':8790'})
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
