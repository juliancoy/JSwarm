"""Offline stress matrix and checks of the production cache ownership policy."""
from dataclasses import replace
import hashlib
import json
import math
from pathlib import Path
import random
import sqlite3
import tempfile
import time

from . import swarm_prerelease as sim


def queue_policy_audit(seeds, policy=None):
    """Compare real production decisions with explicit ownership manifests.

    Only temporary SQLite files are used. No Docker calls or live queues occur.
    The optional callable enables a negative control of the checker itself.
    """
    if policy is None:
        from .swarm_shared_cache import finished_cache_keys
        policy = finished_cache_keys
    failures = []
    checks = 0
    hosts = [f'node-{index}' for index in range(5)]
    with tempfile.TemporaryDirectory(prefix='jswarm-queue-audit-') as folder:
        path = Path(folder) / 'queue.sqlite'
        with sqlite3.connect(path) as db:
            db.execute('CREATE TABLE submissions(state TEXT, host TEXT, request TEXT)')

        def check(seed, case, expected):
            nonlocal checks
            for host in hosts:
                actual = policy({'host': host + '#cpu'}, path)
                checks += 1
                if actual != expected[host]:
                    failures.append({'seed': seed, 'case': case, 'host': host,
                                     'expected': sorted(expected[host]), 'actual': sorted(actual)})

        def save(rows):
            with sqlite3.connect(path) as db:
                db.execute('DELETE FROM submissions')
                db.executemany('INSERT INTO submissions VALUES(?,?,?)', rows)

        for seed in seeds:
            rng = random.Random(seed)
            keys = [hashlib.sha256(f'{seed}:{index}'.encode()).hexdigest() for index in range(12)]
            terminal = {host: set() for host in hosts}
            active = {host: set() for host in hosts}
            rows = []
            for index in range(60):
                state = rng.choice(('completed', 'failed', 'cancelled', 'accepted', 'preparing', 'queued', 'unknown'))
                coordinator = rng.choice(hosts)
                bindings = []
                for _ in range(rng.randrange(1, 4)):
                    owner, key = rng.choice(hosts), rng.choice(keys)
                    (terminal if state in ('completed', 'failed', 'cancelled') else active)[owner].add(key)
                    bindings.append({'host': owner + rng.choice(('', '#cpu', '#gpu')), 'key': key,
                                     'container': 'swarm-job-' + key[:32], 'volume': 'swarm-cache-' + key[:32]})
                rows.append((state, coordinator, json.dumps({'inputs_ready': False, 'cache_claims_version': 1,
                             'inputs': {'source': [bindings[0]], 'witnesses': {'nested': bindings[1:]}}})))
            expected = {host: terminal[host] - active[host] for host in hosts}
            save(rows)
            check(seed, 'modern_nested_ownership', expected)
            # A terminal-only key proves cleanup can make progress, even with
            # unfinished preparation on another physical machine.
            key = hashlib.sha256(f'{seed}:terminal-only'.encode()).hexdigest()
            binding = {'key': key, 'container': 'swarm-job-' + key[:32], 'volume': 'swarm-cache-' + key[:32]}
            terminal_row = ('completed', hosts[0], json.dumps({'cache_inputs': [binding]}))
            for owner, case in ((hosts[1], 'legacy_other_node'), (hosts[0] + '#gpu', 'legacy_same_node'), (None, 'legacy_unknown_node')):
                save([terminal_row, ('preparing', owner, json.dumps({'inputs_ready': False}))])
                wanted = {host: set() for host in hosts}
                if owner == hosts[1]:
                    wanted[hosts[0]] = {key}
                check(seed, case, wanted)
            save([terminal_row, ('accepted', hosts[0], '{invalid-json')])
            check(seed, 'corrupt_queue_fails_closed', {host: set() for host in hosts})
            invalid = {**binding, 'volume': 'unverified-volume'}
            save([('completed', hosts[0], json.dumps({'cache_inputs': [invalid]}))])
            check(seed, 'unverified_binding_is_not_reclaimable', {host: set() for host in hosts})
    return {'checks': checks, 'failures': failures,
            'scope': 'Actual finished_cache_keys against temporary randomized SQLite queues; no Docker eviction or production scheduler execution.'}


def stress_settings(base):
    return {
        'baseline': base,
        'memory-pressure': replace(base, memory_factor=base.memory_factor * 1.4,
                                   background_fraction=max(.3, base.background_fraction), cache_mb=base.cache_mb * 2),
        'cache-churn': replace(base, dataset_pool=64, cache_ttl=max(base.cache_ttl * 4, 1)),
        'slow-transfers': replace(base, transfer_mb_s=base.transfer_mb_s / 10),
        'failure-recovery': replace(base, dependency_rate=.6, cancellation_rate=.15, restart_rate=1,
                                    failure_rate=.2, outage_seconds=max(90, base.outage_seconds)),
        'long-soak': replace(base, jobs=base.jobs * 2, waves=base.waves * 4, dataset_pool=16),
    }


def suite(profile, settings, seeds, ownership_seeds=20, max_regression_percent=None):
    seeds = list(seeds)
    if not seeds or ownership_seeds < 1:
        raise ValueError('Positive experiment and ownership seed counts required')
    if max_regression_percent is not None and (not isinstance(max_regression_percent, (int, float))
                                              or max_regression_percent < 0 or not math.isfinite(max_regression_percent)):
        raise ValueError('Runtime regression threshold must be finite and nonnegative')
    started = time.perf_counter()
    ownership = queue_policy_audit(range(seeds[0], seeds[0] + ownership_seeds))
    reports = {name: sim.experiments(profile, variant, seeds) for name, variant in stress_settings(settings).items()}
    shard_coverage = None
    if profile.get('shard_history'):
        history = profile['shard_history']
        suffixes = {'call': 'calling', 'compare': 'comparison', 'compare-references': 'comparison',
                    'project': 'projection', 'phase': 'phase', 'structural': 'structural',
                    'aggregate': 'report', 'boundaries': 'audit'}
        families = sorted({sample['family'] for node in profile['nodes'] for sample in node['samples']})
        mapping = {family: suffixes[family.rsplit('/', 1)[-1]] for family in families if family.rsplit('/', 1)[-1] in suffixes}
        counts = history['completed_stages']
        shape = {'family_weights': {family: counts.get(method, 0) for family, method in mapping.items()},
                 'cache_budget_mb_by_family': {family: history['worker_cache_budget_mb_by_method'].get(method, []) for family, method in mapping.items()},
                 'fan_in_by_family': {family: history.get('dependency_fan_in_by_method', {}).get(method, [1]) for family, method in mapping.items()}}
        reports['completed-shards'] = sim.experiments({**profile, 'workload_shape': shape}, replace(settings, dependency_rate=1), seeds)
        shard_coverage = {'modeled_methods': sorted(set(mapping.values()) & set(counts)),
                          'unmodeled_methods': sorted(set(counts) - set(mapping.values())),
                          'completed_stages': counts, 'source': history.get('source'), 'limitations': history.get('limitations')}
    regressions = []
    if max_regression_percent is not None:
        for name, report in reports.items():
            for result in report['experiments']:
                if result['makespan_improvement_percent'] < -max_regression_percent:
                    regressions.append({'scenario': name, 'seed': result['seed'],
                                        'regression_percent': -result['makespan_improvement_percent']})
    failed = sum(report['summary']['failed_runs'] for report in reports.values())
    virtual = sum(report['summary']['virtual_seconds'] for report in reports.values())
    elapsed = time.perf_counter() - started
    return {'schema': sim.SCHEMA, 'scenarios': reports, 'production_ownership': ownership, 'completed_shard_coverage': shard_coverage,
            'runtime_regression_threshold_percent': max_regression_percent, 'runtime_gate_violations': regressions,
            'summary': {'scenarios': len(reports), 'policy_runs': sum(r['summary']['policy_runs'] for r in reports.values()),
                        'failed_runs': failed, 'production_ownership_checks': ownership['checks'],
                        'production_ownership_failures': len(ownership['failures']), 'runtime_gate_violations': len(regressions),
                        'runtime_gate_enabled': max_regression_percent is not None,
                        'candidate_regression_pairs': sum(r['summary']['candidate_regressions'] for r in reports.values()),
                        'passed': not (failed or ownership['failures'] or regressions),
                        'wall_seconds': elapsed, 'virtual_seconds': virtual, 'time_compression': virtual / max(elapsed, 1e-9)}}
