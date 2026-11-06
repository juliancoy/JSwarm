"""Clean terminal-only shared RAM caches across enrolled JSwarm machines.

Defaults to a dry run. --apply removes only labeled cache owners/volumes whose
bindings have no unfinished queue consumers and no Docker readers. All node
inspection/removal uses resource.caches.inspect/cleanup RPCs, never node SSH,
arbitrary command execution, or prune.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import sys
import types

SCHEMA = 1


def load_policy(source):
    """Private policy instance; never patch a live controller's module globals."""
    module = types.ModuleType('swarm_cleanup_policy')
    module.__file__ = str(Path(__file__).with_name('swarm_shared_cache.py'))
    exec(compile(source, module.__file__, 'exec'), module.__dict__)
    return module


def classify(rows, eligible, uses):
    result = []
    for row in sorted(rows, key=lambda row: (uses.get(row['key'], 0), row.get('created', ''), row['key'])):
        reason = ('volume_not_verified' if not row['volume_verified'] else
                  'owner_mount_not_verified' if not row.get('owner_verified', False) else
                  'unfinished_or_unknown_ownership' if row['key'] not in eligible else
                  'docker_consumer' if row['consumers'] else
                  'active_transfer_or_exec' if row['exec_readers'] else 'eligible_terminal_cache')
        result.append({**row, 'reason': reason, 'eligible': reason == 'eligible_terminal_cache'})
    return result


def status_summary(client, name):
    status = client.call('status', timeout=20)
    if status.get('node') != name or not status.get('machine'):
        raise ValueError('Node identity unavailable or different')
    if 'cache-cleanup-v1' not in status.get('capabilities', []):
        raise ValueError('Node requires the standard update with cache-cleanup-v1; no command/SSH fallback')
    workers = status.get('workers')
    if not isinstance(workers, list):
        raise ValueError('Worker inventory unavailable')
    caches = [worker for worker in workers if worker.get('group') == 'shared-cache' and worker.get('active') is True]
    capacity = client.call('resource.capacity', timeout=20)
    available = capacity.get('available_mb')
    if not isinstance(available, (int, float)) or isinstance(available, bool) or not math.isfinite(available) or available < 0:
        raise ValueError('Admission headroom unavailable')
    return {'machine': status['machine'], 'cache_workers': len(caches),
            'other_workers': sum(worker.get('active') is True and worker.get('group') != 'shared-cache' for worker in workers),
            'available_mb': available}


def clean_node(name, client, policy, queue, apply=False, min_free_mb=None):
    row = {'node': name, 'mode': 'apply' if apply else 'dry-run', 'removed': [], 'skipped': [], 'errors': []}
    try:
        row['before'] = status_summary(client, name)
        inventory = client.call('resource.caches.inspect', timeout=120)['items']
        eligible, uses = policy._ownership_for({'host': name}, queue)
        row['inventory'] = classify(inventory, eligible, uses)
        row['eligible_at_inspection'] = sum(cache['eligible'] for cache in row['inventory'])
        if not apply:
            row['status'] = 'preview'
            return row
        verified = status_summary(client, name)
        if verified['machine'] != row['before']['machine']:
            raise ValueError('Machine identity changed during inspection')
        for cache in row['inventory']:
            if not cache['eligible']:
                continue
            if min_free_mb is not None:
                capacity = client.call('resource.capacity', timeout=20)
                available = capacity.get('available_mb')
                if not isinstance(available, (int, float)) or isinstance(available, bool) or not math.isfinite(available) or available < 0:
                    raise ValueError('Admission headroom unavailable during cleanup')
                if available >= min_free_mb:
                    row['stopped_reason'] = 'headroom_target_met'
                    break
            # Queue authority stays on the controller. Re-read immediately
            # before each dedicated API cleanup request; never trust a saved dry run.
            if cache['key'] not in policy.finished_cache_keys({'host': name}, queue):
                row['skipped'].append({'key': cache['key'], 'reason': 'ownership_changed_or_protected'})
                continue
            outcome = client.call('resource.caches.cleanup', {
                'key': cache['key'], 'owner_id': cache['owner_id'], 'machine': verified['machine'],
                'ownership': 'terminal-only', 'dry_run': False}, timeout=120)
            if outcome.get('removed') is True:
                row['removed'].append({'key': cache['key'], 'container': cache['container'], 'volume': cache['volume'],
                                       'owner_id': cache['owner_id']})
            else:
                row['skipped'].append({'key': cache['key'], 'reason': outcome.get('reason', 'not_removed'),
                                       'owner_removed': outcome.get('owner_removed', False)})
        row['after'] = status_summary(client, name)
        if row['after']['machine'] != row['before']['machine']:
            raise ValueError('Machine identity changed after cleanup')
        row['status'] = 'complete'
    except Exception as error:
        row['status'] = 'error'
        row['errors'].append(type(error).__name__ + ': ' + str(error)[:300])
    return row


def cleanup_fleet(policy_source, hosts=None, apply=False, min_free_mb=None, queue=None):
    root = Path(__file__).resolve().parents[2]
    sys.path[:0] = [str(root), str(root / 'JSwarm/swarm-control/server')]
    from agent_client import AgentClient, CONFIG
    config = json.loads(CONFIG.read_text())
    names = sorted(hosts or config['peers'])
    if not names or len(set(names)) != len(names) or any(name not in config['peers'] for name in names):
        raise ValueError('Select distinct enrolled machine names')
    queue = Path(queue or Path.home() / '.local/state/swarm-control/submission/queue.sqlite').resolve()
    if not queue.is_file():
        raise ValueError('Controller submission queue is missing; refusing cleanup')
    policy = load_policy(policy_source)
    # Strict preflight distinguishes an unreadable queue from "nothing eligible".
    reader = policy._QueueOwnership(queue, None)
    try:
        reader.refresh()
    finally:
        reader.db.close()

    def one(name):
        client = None
        try:
            client = AgentClient({'host': name, 'agent_route': 'auto',
                                  'agent_endpoint': 'http://' + config['peers'][name] + ':8790'})
            return clean_node(name, client, policy, queue, apply, min_free_mb)
        except Exception as error:
            return {'node': name, 'mode': 'apply' if apply else 'dry-run', 'status': 'error',
                    'removed': [], 'skipped': [], 'errors': [type(error).__name__ + ': ' + str(error)[:300]]}
        finally:
            if client is not None:
                client.close()

    with ThreadPoolExecutor(max_workers=min(5, len(names))) as pool:
        rows = list(pool.map(one, names))
    return {'schema': SCHEMA, 'sampled_at': datetime.now(timezone.utc).isoformat(), 'apply': apply,
            'policy_sha256': hashlib.sha256(policy_source.encode()).hexdigest(), 'min_free_mb': min_free_mb,
            'machines': rows, 'summary': {'machines': len(rows), 'failed_machines': sum(bool(row['errors']) for row in rows),
                                         'eligible_at_inspection': sum(row.get('eligible_at_inspection', 0) for row in rows),
                                         'removed': sum(len(row['removed']) for row in rows)},
            'scope': 'Shared cache owners and labeled tmpfs volumes only. No package caches, staging directories, job results, images, or global prune.'}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--controller', help='Bootstrap on the signing controller using existing host-pinned access')
    parser.add_argument('--hosts', nargs='+', help='Default: all enrolled machines')
    parser.add_argument('--apply', action='store_true', help='Perform eligible removals; default is dry-run')
    parser.add_argument('--min-free-mb', type=int, help='Stop each machine once observed admission headroom meets this target')
    parser.add_argument('--queue', help='Existing queue path on the controller')
    parser.add_argument('--output', required=True)
    args = parser.parse_args(argv)
    if args.min_free_mb is not None and args.min_free_mb < 1:
        parser.error('--min-free-mb must be positive')
    policy_source = Path(__file__).with_name('swarm_shared_cache.py').read_text()
    if args.controller:
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'swarm-control/server'))
        from interface_metrics import capture
        source = Path(__file__).read_text()
        script = ("from pathlib import Path\n" +
                  "scope={'__name__':'cache_cleanup','__file__':str(Path.home()/'Documents/Organization/JSwarm/scripts/swarm_cache_cleanup.py')}\n" +
                  f'exec({source!r},scope)\n' +
                  f'result=scope["cleanup_fleet"]({policy_source!r},{args.hosts!r},{args.apply!r},{args.min_free_mb!r},{args.queue!r})\n' +
                  'import json,zlib,base64\nprint(json.dumps({"compressed":base64.b64encode(zlib.compress(json.dumps(result).encode())).decode()}))\n')
        import base64
        import zlib
        response = capture({'host': args.controller}, script, timeout=7200)
        result = json.loads(zlib.decompress(base64.b64decode(response['compressed'])))
    else:
        result = cleanup_fleet(policy_source, args.hosts, args.apply, args.min_free_mb, args.queue)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, indent=2, allow_nan=False) + '\n')
    for row in result['machines']:
        print(f"{row['node']}: {row['status']}; eligible={row.get('eligible_at_inspection', '?')}; removed={len(row['removed'])}; skipped={len(row['skipped'])}")
        for error in row['errors']:
            print('  ' + error)
    print('Report: ' + str(output))
    return int(result['summary']['failed_machines'] > 0)


if __name__ == '__main__':
    raise SystemExit(main())
