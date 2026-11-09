"""Read-only audit of every enrolled node through authenticated JSwarm APIs.

Collects status, resource.capacity and resource.caches. Never submits jobs,
executes remote commands, removes caches, or changes worker policy. An optional
controller bootstrap uses existing host-pinned access to run these API reads
where the controller signing key is installed.
"""
import argparse
import base64
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import sys
import time
import zlib

MIB = 1024 ** 2
SCHEMA = 1


def number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0


def total(rows, key):
    values = [row.get(key) for row in rows]
    return sum(values) if all(number(value) for value in values) else None


def summarize(host, status, capacity, inventory, errors, sampled_at, max_memory_age=60):
    workers = [row for row in status.get('workers', []) if row.get('active') is True]
    caches = [row for row in workers if row.get('group') == 'shared-cache']
    other = [row for row in workers if row.get('group') != 'shared-cache']
    groups = Counter(row.get('group', 'unknown') for row in workers)
    fresh = []
    for row in caches:
        observation = row.get('memory_observation') or {}
        stamp = observation.get('sampled_at')
        age = status.get('time', sampled_at) - stamp if number(stamp) else None
        if age is not None and -1 <= age <= max_memory_age and number(observation.get('raw_bytes')):
            fresh.append(observation['raw_bytes'] / MIB)
    verified = inventory.get('caches', []) if inventory is not None else None
    pending = inventory.get('pending_caches', []) if inventory is not None else None
    payload_mb = None
    duplicates = []
    if verified is not None:
        payload_mb = 0
        signatures = defaultdict(list)
        for row in verified:
            files = row.get('files', {})
            size = total(list(files.values()), 'bytes')
            if size is None:
                payload_mb = None
                errors['resource.caches.payload'] = 'Missing or invalid verified file sizes'
                continue
            if payload_mb is not None:
                payload_mb += size / MIB
            signature = hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()
            signatures[signature].append({'container': row.get('container'), 'image': row.get('image'), 'mb': size / MIB})
        for copies in signatures.values():
            if len(copies) > 1:
                duplicates.append({'copies': len(copies), 'images': len({row['image'] for row in copies}),
                                   'repeated_logical_payload_mb': sum(row['mb'] for row in copies[1:]),
                                   'containers': [row['container'] for row in copies]})
    storage = status.get('storage') or {}
    storage_metrics = {key: storage.get(key) for key in (
        'retained_bytes', 'workspace_budget_bytes', 'package_cache_bytes', 'free_bytes', 'minimum_free_bytes', 'checked_at')}
    if storage.get('error'):
        errors['storage'] = str(storage['error'])[:300]
    warnings = []
    available, memory = capacity.get('available_mb'), capacity.get('memory_mb')
    if number(available) and number(memory) and available < max(1024, memory * .1):
        warnings.append('low_admission_headroom')
        if caches and not other and not status.get('active_jobs'):
            warnings.append('cache_pressure_without_other_workers')
    if len(fresh) != len(caches):
        warnings.append('cache_memory_measurements_incomplete_or_stale')
    if number(storage.get('retained_bytes')) and number(storage.get('workspace_budget_bytes')) and storage['retained_bytes'] > storage['workspace_budget_bytes']:
        warnings.append('retained_workspace_budget_exceeded')
    if number(storage.get('free_bytes')) and number(storage.get('minimum_free_bytes')) and storage['free_bytes'] < storage['minimum_free_bytes']:
        warnings.append('storage_free_below_floor')
    if number(storage.get('checked_at')) and status.get('time', sampled_at) - storage['checked_at'] > 300:
        warnings.append('storage_observation_stale')
    return {'node': host, 'machine': status.get('machine'), 'revision': status.get('source_revision'),
            'observed_at': sampled_at, 'ready': status.get('ready'),
            'accepting_jobs': status.get('worker_policy', {}).get('accepting_jobs'),
            'worker_groups': dict(groups), 'cache_workers': len(caches), 'other_workers': len(other),
            'native_active_jobs': len(status.get('active_jobs', [])), 'preparations': len(status.get('preparations', [])),
            'cache_reserved_mb': total(caches, 'memory_mb'),
            'cache_sampled_raw_mb': sum(fresh) if len(fresh) == len(caches) else None,
            'cache_partial_sampled_raw_mb': sum(fresh), 'cache_memory_samples': len(fresh),
            'verified_caches': len(verified) if verified is not None else None,
            'pending_caches': len(pending) if pending is not None else None,
            'pending_keys': sorted(pending) if pending is not None else None,
            'verified_payload_mb': payload_mb, 'duplicate_payload_groups': duplicates,
            'available_mb': available, 'memory_mb': memory,
            'memory_pressure_some_percent': capacity.get('memory_pressure_some_percent'),
            'memory_pressure_full_percent': capacity.get('memory_pressure_full_percent'),
            'waiting_jobs': capacity.get('waiting_jobs'), 'storage': storage_metrics,
            'errors': dict(errors), 'warnings': warnings}


def collect_node(host, client, timeout=30, inventory_timeout=120):
    errors = {}
    try:
        status = client.call('status', timeout=timeout)
        if status.get('node') != host:
            raise ValueError('Node API identity differs from enrolled target')
        if not isinstance(status.get('workers'), list):
            raise ValueError('Node status does not report a worker inventory')
    except Exception as error:
        return {'node': host, 'observed_at': time.time(), 'errors': {'status': str(error)[:300]}, 'warnings': []}
    observed_at = time.time()
    capacity, inventory = {}, None
    for operation in ('resource.capacity', 'resource.caches'):
        try:
            value = client.call(operation, timeout=inventory_timeout if operation == 'resource.caches' else timeout)
            if operation == 'resource.capacity':
                if not number(value.get('available_mb')) or not number(value.get('memory_mb')):
                    raise ValueError('Capacity does not report valid RAM measurements')
                capacity = value
            else:
                if not isinstance(value.get('caches'), list) or not isinstance(value.get('pending_caches'), list):
                    raise ValueError('Cache inventory does not report verified and pending entries')
                inventory = value
        except Exception as error:
            errors[operation] = str(error)[:300]
    return summarize(host, status, capacity, inventory, errors, observed_at)


def capture_snapshot(hosts=None, timeout=30, inventory_timeout=120):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'swarm-control/server'))
    from agent_client import AgentClient, CONFIG
    config = json.loads(CONFIG.read_text())
    names = sorted(hosts or config['peers'])
    if not names or len(names) != len(set(names)) or any(name not in config['peers'] for name in names):
        raise ValueError('Select distinct enrolled machine names')

    def collect(name):
        client = None
        try:
            client = AgentClient({'host': name, 'agent_route': 'auto',
                                  'agent_endpoint': 'http://' + config['peers'][name] + ':8790'})
            return collect_node(name, client, timeout, inventory_timeout)
        except Exception as error:
            return {'node': name, 'observed_at': time.time(), 'errors': {'connection': str(error)[:300]}, 'warnings': []}
        finally:
            if client is not None:
                client.close()

    with ThreadPoolExecutor(max_workers=min(5, len(names))) as pool:
        nodes = list(pool.map(collect, names))
    return {'sampled_at': time.time(), 'nodes': nodes}


def compare(current, previous):
    before = {row['node']: row for row in previous.get('nodes', [])}
    for row in current['nodes']:
        old = before.get(row['node'])
        if not old or 'cache_workers' not in old or 'cache_workers' not in row:
            continue
        if not old.get('machine') or row.get('machine') != old['machine']:
            row['comparison_unavailable'] = 'Machine identity missing or changed'
            continue
        elapsed = row['observed_at'] - old['observed_at']
        if elapsed <= 0:
            row['comparison_unavailable'] = 'Previous observation is not older'
            continue
        delta = {'elapsed_seconds': elapsed}
        for key in ('cache_workers', 'cache_reserved_mb', 'cache_sampled_raw_mb', 'available_mb', 'verified_payload_mb'):
            delta[key] = row[key] - old[key] if number(row.get(key)) and number(old.get(key)) else None
        if row.get('pending_keys') is not None and old.get('pending_keys') is not None:
            delta['pending_keys_still_present'] = len(set(row['pending_keys']) & set(old['pending_keys']))
        if delta['cache_workers'] > 0 and delta['available_mb'] is not None and delta['available_mb'] < 0:
            row['warnings'].append('cache_count_grew_while_headroom_fell')
        row['change'] = delta


def report(snapshots):
    latest = snapshots[-1]
    return {'schema': SCHEMA, 'sampled_at': datetime.fromtimestamp(latest['sampled_at'], timezone.utc).isoformat(),
            'snapshots': snapshots, 'summary': {
                'machines': len(latest['nodes']), 'status_unavailable': sum('cache_workers' not in row for row in latest['nodes']),
                'partial_machines': sum(bool(row['errors']) for row in latest['nodes']),
                'flagged_machines': sum(bool(row['warnings']) for row in latest['nodes'])},
            'limitations': [
                'All machine observations use authenticated status, resource.capacity and resource.caches RPCs; no command or cleanup API is invoked.',
                'Different RPCs are sequential live observations, not an atomic snapshot.',
                'Reservations, fresh sampled raw RAM, and verified logical payload bytes are separate quantities; missing RAM measurements remain unknown.',
                'The cache inventory exposes running owners only; stopped/orphan volumes, unattached tmpfs staging and image/layer caches are outside these APIs.',
                'Queue ownership and Docker consumer bindings are not exposed by these RPCs; retained caches are not thereby proven unnecessary.',
                'Package-cache and retained-workspace sizes are the last maintenance observations and may be stale.',
                'Short-term growth can reflect legitimate workload; a leak requires sustained growth and ownership/lifecycle evidence.']}


def print_table(data):
    print('Machine                         Caches   Reserved MiB   Sampled RAM MiB   Available MiB   Other workers')
    def display(value):
        return str(round(value)) if number(value) else '?'
    for row in data['snapshots'][-1]['nodes']:
        print(f"{row['node']:<31} {display(row.get('cache_workers')):>6} {display(row.get('cache_reserved_mb')):>14} "
              f"{display(row.get('cache_sampled_raw_mb')):>17} {display(row.get('available_mb')):>15} {display(row.get('other_workers')):>15}")
        if row['warnings']:
            print('  Flags: ' + ', '.join(row['warnings']))
        if row['errors']:
            print('  Unavailable: ' + ', '.join(sorted(row['errors'])))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--controller', help='Bootstrap API reads on the signing controller using existing host-pinned access')
    parser.add_argument('--hosts', nargs='+', help='Default: every enrolled machine')
    parser.add_argument('--output', required=True)
    parser.add_argument('--previous', help='Earlier JSON report for growth comparison')
    parser.add_argument('--samples', type=int, default=1)
    parser.add_argument('--interval', type=float, default=30, help='Seconds between completed samples')
    parser.add_argument('--timeout', type=float, default=30, help='Per API attempt; the normal client may retry safe reads')
    parser.add_argument('--inventory-timeout', type=float, default=120, help='Allow slower verified-cache enumeration its own deadline')
    args = parser.parse_args(argv)
    if args.samples < 1 or not number(args.interval) or not number(args.timeout) or args.timeout == 0 or not number(args.inventory_timeout) or args.inventory_timeout == 0:
        parser.error('Positive samples/timeout and finite nonnegative interval required')
    previous = None
    if args.previous:
        previous_report = json.loads(Path(args.previous).read_text())
        if previous_report.get('schema') != SCHEMA:
            parser.error('Unsupported previous audit schema')
        previous = previous_report['snapshots'][-1]
    snapshots = []
    for index in range(args.samples):
        if index:
            time.sleep(args.interval)
        if args.controller:
            sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'swarm-control/server'))
            from interface_metrics import capture
            source = Path(__file__).read_text()
            script = ("from pathlib import Path\n" +
                      "scope={'__name__':'cache_audit','__file__':str(Path.home()/'Documents/Organization/JSwarm/scripts/swarm_cache_audit.py')}\n" +
                      f'exec({source!r},scope)\n' + f'result=scope["capture_snapshot"]({args.hosts!r},{args.timeout!r},{args.inventory_timeout!r})\n' +
                      'import json,zlib,base64\nprint(json.dumps({"compressed":base64.b64encode(zlib.compress(json.dumps(result).encode())).decode()}))\n')
            response = capture({'host': args.controller}, script, timeout=max(90, args.timeout * 6 + args.inventory_timeout + 30))
            snapshot = json.loads(zlib.decompress(base64.b64decode(response['compressed'])))
        else:
            snapshot = capture_snapshot(args.hosts, args.timeout, args.inventory_timeout)
        if previous is not None:
            compare(snapshot, previous)
        snapshots.append(snapshot)
        previous = snapshot
        # Preserve completed observations even if a subsequent sample fails.
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report(snapshots), indent=2, allow_nan=False) + '\n')
    data = report(snapshots)
    print_table(data)
    print('JSON report: ' + str(output))
    return 2 if data['summary']['partial_machines'] else int(data['summary']['flagged_machines'] > 0)


if __name__ == '__main__':
    raise SystemExit(main())
