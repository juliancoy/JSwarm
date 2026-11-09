"""Read-only, aggregate workload inspiration from completed coordinator shards."""
import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import sqlite3
import sys

MIB = 1024 ** 2


def summarize(queue, manifest=None):
    jobs = {job['id']: job for job in json.loads(Path(manifest).read_text()).get('jobs', [])} if manifest else {}
    stages = Counter()
    footprints = defaultdict(list)
    bindings_per_job = []
    fan_in = []
    fan_in_by_method = defaultdict(list)
    cache_references = Counter()
    durations = defaultdict(list)
    counts = Counter()
    with sqlite3.connect(Path(queue).resolve().as_uri() + '?mode=ro', uri=True, timeout=3) as db:
        for ident, host, request, result in db.execute(
                "SELECT id,host,request,result FROM submissions WHERE state='completed' AND namespace LIKE 'genomics-followup:%' ORDER BY id"):
            req, res = json.loads(request or '{}'), json.loads(result or '{}')
            method = res.get('method')
            if not method:
                counts['without_method'] += 1
                continue
            stages[method] += 1
            spec = jobs.get(ident)
            if spec is not None:
                degree = len(spec.get('depends_on', []))
                fan_in.append(degree)
                fan_in_by_method[method].append(degree)
            worker = (req.get('worker_host') or host or '').split('#', 1)[0]
            unique = {}

            def visit(value, owner):
                if isinstance(value, dict):
                    owner = value.get('host', owner)
                    key = value.get('key')
                    size = value.get('memory_mb')
                    if (isinstance(key, str) and len(key) == 64 and owner and owner.split('#', 1)[0] == worker
                            and isinstance(size, (int, float)) and math.isfinite(size) and size > 0
                            and value.get('volume') == 'swarm-cache-' + key[:32]):
                        unique[key] = size
                    for child in value.values():
                        visit(child, owner)
                elif isinstance(value, list):
                    for child in value:
                        visit(child, owner)

            visit(req, host)
            if unique:
                counts['with_cache_bindings'] += 1
                footprints[method].append(sum(unique.values()))
                bindings_per_job.append(len(unique))
                cache_references.update(unique.keys())
            # Sample bounded execution receipts to inspect duration distributions.
            # These do not replace calibration from the individual worker APIs.
            if len(durations[method]) < 128 and req.get('directory'):
                try:
                    receipt = json.loads((Path(req['directory']) / 'execution.json').read_text())
                    elapsed = receipt.get('elapsed_seconds')
                    if isinstance(elapsed, (int, float)) and math.isfinite(elapsed) and elapsed > 0:
                        durations[method].append(elapsed)
                except (OSError, ValueError):
                    counts['unreadable_execution_receipts'] += 1
    if not stages:
        raise ValueError('No completed follow-up shards with result methods')

    def bounded(values):
        values = sorted(values)
        return values if len(values) <= 128 else [values[index * (len(values) - 1) // 127] for index in range(128)]

    return {'schema': 1, 'captured_at': datetime.now(timezone.utc).isoformat(),
            'source': 'Completed follow-up queue receipts, current manifest, and bounded execution receipt samples',
            'completed_stages': dict(stages), 'coverage': dict(counts),
            'worker_cache_budget_mb_by_method': {key: bounded(values) for key, values in footprints.items()},
            'worker_cache_bindings_per_job': bounded(bindings_per_job), 'dependency_fan_in': bounded(fan_in),
            'dependency_fan_in_by_method': {key: bounded(values) for key, values in fan_in_by_method.items()},
            'cache_consumer_counts': bounded(list(cache_references.values())),
            'execution_seconds_by_method': dict(durations),
            'limitations': ['Cache budgets are recorded owner reservations, not measured resident RAM.',
                            'Per-job aggregate cache budgets conservatively combine shared reference and job inputs.',
                            'Coverage includes only completed work with retained receipts; failure frequencies cannot be inferred.',
                            'Execution receipts inspire workload shape; worker API histories remain the runtime calibration.',
                            'Dependency counts use the current manifest; no specimen IDs, paths or content hashes are exported.']}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--controller', help='Read through existing host-pinned controller access')
    parser.add_argument('--output', required=True)
    args = parser.parse_args(argv)
    if args.controller:
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'swarm-control/server'))
        from interface_metrics import capture
        source = Path(__file__).read_text()
        script = ("scope={'__name__':'shard_inspiration'}\n" + f'exec({source!r},scope)\n' +
                  "from pathlib import Path\nimport json,zlib,base64\nstate=Path.home()/'.local/state/swarm-control'\n" +
                  "pointer=json.loads((state/'genomics-followup/current.json').read_text())\n" +
                  "result=scope['summarize'](state/'submission/queue.sqlite',pointer['manifest'])\n" +
                  "print(json.dumps({'compressed':base64.b64encode(zlib.compress(json.dumps(result).encode())).decode()}))\n")
        import base64
        import zlib
        response = capture({'host': args.controller}, script, timeout=60)
        result = json.loads(zlib.decompress(base64.b64decode(response['compressed'])))
    else:
        state = Path.home() / '.local/state/swarm-control'
        pointer = json.loads((state / 'genomics-followup/current.json').read_text())
        result = summarize(state / 'submission/queue.sqlite', pointer['manifest'])
    path = Path(args.output)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, indent=2, allow_nan=False) + '\n')
    print(json.dumps({'completed_stages': result['completed_stages'], 'coverage': result['coverage']}))


if __name__ == '__main__':
    main()
