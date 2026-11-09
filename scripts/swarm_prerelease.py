"""Read-only API calibration and seeded, virtual-time swarm prerelease experiments.

This is a capacity/cache model, not an execution of the production scheduler.
No Docker, wall-clock sleeps, remote jobs, or cache mutations occur during a run.
"""
from __future__ import annotations

import argparse
import base64
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import heapq
import json
import math
from pathlib import Path
import random
import statistics
import sys
import time
import zlib

SCHEMA = 1
MIB = 1024 ** 2


def positive(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value > 0


def family(row):
    """Keep execution stages separate; strip specimen IDs, paths and job IDs."""
    run = row.get('run', '')
    for stage in ('staging-execution', 'extraction', 'compare-references', 'call-', 'mate-prep', 'compare-', 'project-', 'phase-', 'structural-', 'aggregate-', 'boundaries-'):
        if stage in run:
            return row.get('group', 'unknown') + '/' + stage.rstrip('-')
    return row.get('group', 'unknown') + '/worker'


def measurement(row):
    if row.get('running') or row.get('exit_code') != 0 or row.get('sink', 'cpu') != 'cpu':
        return None
    elapsed = row.get('elapsed_seconds')
    if not positive(elapsed):
        try:
            elapsed = (datetime.fromisoformat(row['finished_at'].replace('Z', '+00:00')) -
                       datetime.fromisoformat(row['started_at'].replace('Z', '+00:00'))).total_seconds()
        except (KeyError, TypeError, ValueError):
            return None
    peak = row.get('memory_peak_bytes')
    coverage = row.get('memory_accounting_coverage')
    # A whole-host memory limit is not a measurement of worker usage.
    if not positive(elapsed) or not positive(peak) or coverage not in ('final-pre-exit', 'sampled-partial'):
        return None
    cpus = row.get('cpus')
    if not positive(cpus):
        return None
    return {'family': family(row), 'seconds': elapsed, 'cpus': cpus,
            'memory_mb': peak / MIB, 'memory_coverage': coverage,
            'cpu_coverage': row.get('cpu_accounting_coverage', 'unavailable'),
            'cpu_seconds': row.get('cpu_core_seconds') if positive(row.get('cpu_core_seconds')) else None}


def calibrate(host, client, max_pages=200, samples_per_family=64):
    capacity = client.call('resource.capacity', timeout=30)
    if not positive(capacity.get('cpus')) or not positive(capacity.get('memory_mb')):
        raise ValueError(f'{host}: invalid resource capacity')
    buckets = defaultdict(list)
    counts = Counter()
    cursor = ''
    seen = set()
    complete = False
    oldest = newest = None
    for _ in range(max_pages):
        page = client.call('worker.records', {'after': cursor}, timeout=30)
        for row in page['items']:
            counts['records'] += 1
            if row.get('running'):
                counts['running'] += 1
            elif row.get('exit_code') != 0:
                counts['failed_or_unknown'] += 1
            if row.get('sink') == 'gpu':
                counts['gpu_excluded'] += 1
            sample = measurement(row)
            if sample is None:
                continue
            counts['usable'] += 1
            counts['memory_' + sample['memory_coverage']] += 1
            stamp = row.get('finished_at', row.get('finished'))
            if stamp:
                oldest = min(oldest or stamp, stamp)
                newest = max(newest or stamp, stamp)
            # Deterministic bounded sampling, independent of API page ordering.
            rank = hashlib.sha256(str(row['job']).encode()).hexdigest()
            bucket = buckets[sample['family']]
            bucket.append((rank, sample))
            bucket.sort(key=lambda pair: pair[0])
            del bucket[samples_per_family:]
        next_cursor = page.get('next')
        if not next_cursor:
            complete = True
            break
        if next_cursor in seen or next_cursor <= cursor:
            raise ValueError(f'{host}: worker journal cursor did not advance')
        seen.add(next_cursor)
        cursor = next_cursor
    if not complete:
        raise ValueError(f'{host}: journal exceeds {max_pages} pages; increase --max-pages')
    samples = [sample for key in sorted(buckets) for _, sample in buckets[key]]
    if not samples:
        raise ValueError(f'{host}: no successful CPU history with measured RAM; refusing invented calibration')
    return {'host': host, 'cpus': capacity['cpus'], 'memory_mb': capacity['memory_mb'],
            'available_mb_at_capture': capacity.get('available_mb'), 'history': dict(counts),
            'history_start': oldest, 'history_end': newest, 'samples': samples}


def capture_profiles(hosts=None, max_pages=200):
    # Imported only when explicitly capturing, so replay needs no credentials/dependencies.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'swarm-control/server'))
    from agent_client import AgentClient, CONFIG
    config = json.loads(CONFIG.read_text())
    names = sorted(hosts or config['peers'])
    if len(names) != 5 or len(set(names)) != 5:
        raise ValueError('Select exactly five distinct enrolled machines using --hosts')

    def collect(name):
        if name not in config['peers']:
            raise ValueError(f'Unknown enrolled machine: {name}')
        client = AgentClient({'host': name, 'agent_endpoint': 'http://' + config['peers'][name] + ':8790'})
        try:
            return calibrate(name, client, max_pages)
        finally:
            client.close()

    with ThreadPoolExecutor(max_workers=5) as pool:
        nodes = list(pool.map(collect, names))
    return {'schema': SCHEMA, 'captured_at': datetime.now(timezone.utc).isoformat(),
            'source': 'Each enrolled machine: resource.capacity + paginated worker.records', 'nodes': nodes}


@dataclass(frozen=True)
class Settings:
    jobs: int = 300
    waves: int = 6
    cache_mb: float = 256
    transfer_mb_s: float = 100
    cache_ttl: float = 120
    memory_factor: float = 1.25
    background_fraction: float = .15
    failure_rate: float = .03
    pause_seconds: float = 60
    dependency_rate: float = 0
    cancellation_rate: float = 0
    restart_rate: float = 0
    outage_seconds: float = 90
    dataset_pool: int = 8
    leak: bool = False
    global_guard: bool = False


def validate(profile, settings):
    if profile.get('schema') != SCHEMA or len(profile.get('nodes', [])) != 5:
        raise ValueError('Expected a version 1 calibration with five nodes')
    if len({node['host'] for node in profile['nodes']}) != 5:
        raise ValueError('Duplicate machine in calibration')
    if settings.jobs < 1 or settings.waves < 1 or settings.dataset_pool < 1:
        raise ValueError('Jobs and waves must be positive')
    for name in ('cache_mb', 'transfer_mb_s', 'memory_factor'):
        if not positive(getattr(settings, name)):
            raise ValueError(f'{name} must be finite and positive')
    for name in ('cache_ttl', 'pause_seconds', 'outage_seconds'):
        value = getattr(settings, name)
        if not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
            raise ValueError(f'{name} must be finite and nonnegative')
    if not 0 <= settings.background_fraction < 1 or not 0 <= settings.failure_rate <= 1:
        raise ValueError('Invalid background fraction or failure rate')
    for name in ('dependency_rate', 'cancellation_rate', 'restart_rate'):
        if not 0 <= getattr(settings, name) <= 1:
            raise ValueError(f'{name} must be between zero and one')
    for node in profile['nodes']:
        if not positive(node['cpus']) or not positive(node['memory_mb']) or not node['samples']:
            raise ValueError('Every node needs positive capacity and measured history')
        for sample in node['samples']:
            if not all(positive(sample.get(key)) for key in ('seconds', 'cpus', 'memory_mb')):
                raise ValueError('Invalid historical sample')


def scenario(profile, seed, settings):
    """One immutable workload shared by all policies, with no policy-dependent RNG."""
    rng = random.Random(seed)
    families = sorted({s['family'] for n in profile['nodes'] for s in n['samples']})
    shape = profile.get('workload_shape', {})
    weights = shape.get('family_weights')
    if weights:
        families = [name for name in families if weights.get(name, 0) > 0]
        if not families:
            raise ValueError('Completed shard methods have no matching worker API stages')
    typical = statistics.median(s['seconds'] for n in profile['nodes'] for s in n['samples'])
    gap = max(typical * 4, settings.cache_ttl * 2, 1)
    jobs = []
    cache_sizes = {}
    for index in range(settings.jobs):
        wave = min(settings.waves - 1, index * settings.waves // settings.jobs)
        dataset = f'{wave}:{rng.randrange(settings.dataset_pool)}'
        cache_sizes.setdefault(dataset, settings.cache_mb * rng.choice((.25, 1, 2, 4)))
        job_family = rng.choices(families, weights=[weights[name] for name in families], k=1)[0] if weights else rng.choice(families)
        budget_samples = shape.get('cache_budget_mb_by_family', {}).get(job_family, [])
        cache_mb = rng.choice(budget_samples) if budget_samples else cache_sizes[dataset]
        # Aggregate shard budgets represent different immutable input sets.
        # Avoid falsely sharing different footprints under one dataset key.
        if budget_samples:
            dataset += ':' + str(cache_mb)
        jobs.append({'id': index, 'arrival': wave * gap + rng.random() * gap * .15,
                     'family': job_family, 'quantile': rng.random(),
                     'scale': rng.uniform(.5, 2), 'dataset': dataset,
                     'cache_mb': cache_mb,
                     'retry': rng.random() < settings.failure_rate})
    for job in jobs:
        degree_samples = shape.get('fan_in_by_family', {}).get(job['family'], [1])
        degree = min(job['id'], rng.choice(degree_samples)) if job['id'] and settings.dependency_rate and rng.random() < settings.dependency_rate else 0
        job['depends_on'] = sorted(rng.sample(range(job['id']), degree))
        if settings.cancellation_rate and rng.random() < settings.cancellation_rate:
            job['cancel_at'] = job['arrival'] + rng.uniform(0, typical * 2)
    restarts = [{'node': index, 'at': rng.uniform(.25, 1.25) * gap, 'duration': settings.outage_seconds}
                for index in range(5) if settings.restart_rate and rng.random() < settings.restart_rate]
    pauses = {n['host']: (rng.random() * gap, settings.pause_seconds) for n in profile['nodes']}
    return {'jobs': sorted(jobs, key=lambda j: (j['arrival'], j['id'])), 'pauses': pauses, 'restarts': restarts}


class Simulation:
    def __init__(self, profile, workload, settings, policy):
        if policy not in ('round-robin', 'cache-aware'):
            raise ValueError('Unknown scheduling policy')
        validate(profile, settings)
        self.settings, self.policy, self.workload = settings, policy, workload
        self.nodes = []
        for node in profile['nodes']:
            samples = defaultdict(list)
            for sample in node['samples']:
                samples[sample['family']].append(sample)
            for rows in samples.values():
                rows.sort(key=lambda sample: sample['seconds'])
            self.nodes.append({'profile': node, 'samples': samples,
                               'estimates': {key: statistics.median(s['seconds'] for s in rows) for key, rows in samples.items()}, 'cache': {}, 'cpu': 0., 'ram': 0.,
                               'peak_cache_mb': 0., 'completed': 0, 'cpu_seconds': 0., 'peak_total_mb': 0.})
        self.now = 0.
        self.last_completion = 0.
        self.events = []
        self.sequence = 0
        self.pending = []
        self.running = {}
        self.completed = set()
        self.cancelled = set()
        self.offline = set()
        self.attempts = Counter()
        self.ends = {}
        self.failures = []
        self.waits = []
        self.turnaround = []
        self.trace = []
        self.stats = Counter()
        self.pointer = 0
        for job in workload['jobs']:
            self.event(job['arrival'], 'arrival', job)
            if 'cancel_at' in job:
                self.event(job['cancel_at'], 'cancel', job['id'])
        for restart in workload.get('restarts', []):
            self.event(restart['at'], 'restart', restart['node'])
            self.event(restart['at'] + restart['duration'], 'recover', restart['node'])
        for start, duration in workload['pauses'].values():
            if duration:
                self.event(start, 'wake', None)
                self.event(start + duration, 'wake', None)

    def event(self, at, kind, data):
        self.sequence += 1
        heapq.heappush(self.events, (at, self.sequence, kind, data))

    def budget(self, node):
        total = node['profile']['memory_mb']
        return max(0, total * (1 - self.settings.background_fraction) - max(256, total * .05))

    @staticmethod
    def cache_ram(node):
        return sum(cache['mb'] for cache in node['cache'].values())

    def spec(self, node, job):
        rows = node['samples'].get(job['family'])
        if not rows:
            return None
        sample = rows[min(int(job['quantile'] * len(rows)), len(rows) - 1)]
        # Empirical wall time already includes I/O and host contention. Do not
        # interpret partial CPU totals as a full execution measurement/speedup.
        return (sample['seconds'] * job['scale'], sample['cpus'],
                max(16., sample['memory_mb'] * self.settings.memory_factor))

    def clean(self, node, need=0., protect=None):
        if self.settings.leak:
            return
        # Negative control for the old fleet-wide unfinished-preparation guard.
        if self.settings.global_guard and self.pending:
            return
        for key, cache in sorted(list(node['cache'].items()), key=lambda pair: pair[1]['idle_since']):
            if cache['users'] or key == protect:
                continue
            expired = self.now >= cache['idle_since'] + self.settings.cache_ttl
            if expired or need > self.budget(node) - node['ram'] - self.cache_ram(node):
                del node['cache'][key]
                self.stats['evictions'] += 1

    def candidate(self, index, job):
        node = self.nodes[index]
        spec = self.spec(node, job)
        if spec is None or index in self.offline:
            return None
        seconds, cpus, ram = spec
        start, duration = self.workload['pauses'][node['profile']['host']]
        if start <= self.now < start + duration:
            return None
        if node['cpu'] + cpus > node['profile']['cpus'] + 1e-9:
            return None
        cache = node['cache'].get(job['dataset'])
        extra = 0 if cache else job['cache_mb']
        # Scheduling probes must not mutate cache state. Evict only after choice.
        reclaimable = 0 if self.settings.leak or (self.settings.global_guard and self.pending) else sum(
            c['mb'] for key, c in node['cache'].items() if not c['users'] and key != job['dataset'])
        if node['ram'] + self.cache_ram(node) + ram + extra - reclaimable > self.budget(node) + 1e-9:
            return None
        prep = max(0, cache['ready'] - self.now) if cache else extra / self.settings.transfer_mb_s
        return seconds + prep, cpus, ram

    def estimate(self, index, job):
        node = self.nodes[index]
        cache = node['cache'].get(job['dataset'])
        prep = max(0, cache['ready'] - self.now) if cache else job['cache_mb'] / self.settings.transfer_mb_s
        # The policy knows stage history, not the eventual sampled duration.
        return node['estimates'][job['family']] * job['scale'] + prep

    def release(self, job_id):
        index, cpus, ram, job = self.running.pop(job_id)
        node = self.nodes[index]
        node['cpu'] -= cpus
        node['ram'] -= ram
        node['cpu_seconds'] -= cpus * max(0, self.ends.pop(job_id) - self.now)
        cache = node['cache'][job['dataset']]
        cache['users'] -= 1
        if not cache['users']:
            if cache['ready'] > self.now:
                del node['cache'][job['dataset']]
                self.stats['abandoned_cache_loads'] += 1
            else:
                cache['idle_since'] = self.now
                self.event(self.now + self.settings.cache_ttl, 'expire', None)
        return job

    def dispatch(self):
        # Resolve cancellation transitively, including descendants arriving later.
        changed = True
        while changed:
            changed = False
            kept = []
            for job in self.pending:
                if any(parent in self.cancelled for parent in job.get('depends_on', [])):
                    self.cancelled.add(job['id'])
                    self.stats['dependency_cancellations'] += 1
                    changed = True
                else:
                    kept.append(job)
            self.pending = kept
        remaining = []
        for job in self.pending:
            if not all(parent in self.completed for parent in job.get('depends_on', [])):
                remaining.append(job)
                continue
            options = [(i, candidate) for i in range(5) if (candidate := self.candidate(i, job)) is not None]
            if not options:
                remaining.append(job)
                continue
            if self.policy == 'round-robin':
                index, spec = min(options, key=lambda pair: (pair[0] - self.pointer) % 5)
                self.pointer = (index + 1) % 5
            else:
                index, spec = min(options, key=lambda pair: (self.estimate(pair[0], job), self.nodes[pair[0]]['cpu'], pair[0]))
            duration, cpus, ram = spec
            node = self.nodes[index]
            missing = job['dataset'] not in node['cache']
            self.clean(node, ram + (job['cache_mb'] if missing else 0), protect=job['dataset'])
            cache = node['cache'].get(job['dataset'])
            if cache is None:
                self.stats['cache_loads'] += 1
                self.stats['cache_loaded_mb'] += job['cache_mb']
                cache = {'mb': job['cache_mb'], 'users': 0,
                         'ready': self.now + job['cache_mb'] / self.settings.transfer_mb_s, 'idle_since': self.now}
                node['cache'][job['dataset']] = cache
            else:
                self.stats['cache_hits'] += 1
            cache['users'] += 1  # Ownership published before preparation begins.
            node['cpu'] += cpus
            node['ram'] += ram
            node['cpu_seconds'] += cpus * duration
            self.running[job['id']] = (index, cpus, ram, job)
            self.waits.append(self.now - job.get('ready_at', job['arrival']))
            self.attempts[job['id']] += 1
            self.ends[job['id']] = self.now + duration
            self.event(self.now + duration, 'done', (job['id'], self.attempts[job['id']]))
            used = node['ram'] + self.cache_ram(node)
            node['peak_total_mb'] = max(node['peak_total_mb'], used)
            node['peak_cache_mb'] = max(node['peak_cache_mb'], self.cache_ram(node))
            if used > self.budget(node) + 1e-6:
                self.failures.append({'kind': 'memory_overcommit', 'host': node['profile']['host'], 'at': self.now})
        self.pending = remaining

    def run(self):
        while self.events:
            at, _, kind, data = heapq.heappop(self.events)
            if kind == 'done' and (data[0] not in self.running or self.attempts[data[0]] != data[1]):
                self.stats['stale_completions_ignored'] += 1
                continue
            self.now = at
            self.stats['events'] += 1
            self.trace.append({'at': round(at, 6), 'event': kind, 'job': data[0] if kind == 'done' else data if kind == 'cancel' else None})
            if len(self.trace) > 32:
                self.trace.pop(0)
            if kind == 'arrival':
                if data['id'] not in self.cancelled:
                    self.pending.append(data)
            elif kind == 'cancel':
                if data not in self.completed and data not in self.cancelled:
                    self.cancelled.add(data)
                    self.stats['cancellations'] += 1
                    self.pending = [job for job in self.pending if job['id'] != data]
                    if data in self.running:
                        self.release(data)
            elif kind == 'restart':
                self.offline.add(data)
                self.stats['node_restarts'] += 1
                for job_id, execution in list(self.running.items()):
                    if execution[0] == data:
                        job = self.release(job_id)
                        self.pending.append({**job, 'ready_at': self.now})
                        self.stats['restart_retries'] += 1
                self.stats['restart_cache_dropped_mb'] += self.cache_ram(self.nodes[data])
                self.nodes[data]['cache'].clear()
            elif kind == 'recover':
                self.offline.discard(data)
                self.stats['node_recoveries'] += 1
            elif kind == 'done':
                self.last_completion = self.now
                job_id = data[0]
                index = self.running[job_id][0]
                node = self.nodes[index]
                job = self.release(job_id)
                if job['retry'] and not job.get('retried'):
                    self.stats['retries'] += 1
                    self.pending.append({**job, 'retried': True, 'ready_at': self.now})
                else:
                    self.completed.add(job_id)
                    node['completed'] += 1
                    self.turnaround.append(self.now - job['arrival'])
            for node in self.nodes:
                self.clean(node)
            self.dispatch()
        if self.pending:
            self.failures.append({'kind': 'admission_stall_or_unplaceable', 'jobs': [j['id'] for j in self.pending]})
        for node in self.nodes:
            residual = self.cache_ram(node)
            if residual:
                self.failures.append({'kind': 'cache_accumulation', 'host': node['profile']['host'], 'mb': residual})
        accounted = len(self.completed) + len(self.cancelled)
        if accounted != len(self.workload['jobs']):
            self.failures.append({'kind': 'job_accounting_mismatch', 'accounted': accounted})
        makespan = self.last_completion
        return {'policy': self.policy, 'completed': len(self.completed), 'cancelled': len(self.cancelled), 'submitted': len(self.workload['jobs']),
                'virtual_seconds': self.now, 'makespan_seconds': makespan, 'p95_wait_seconds': percentile(self.waits, .95),
                'p95_turnaround_seconds': percentile(self.turnaround, .95), 'stats': dict(self.stats),
                'failures': self.failures, 'trace_tail': self.trace if self.failures else [],
                'nodes': [{'host': n['profile']['host'], 'completed': n['completed'], 'peak_cache_mb': n['peak_cache_mb'],
                           'final_cache_mb': self.cache_ram(n), 'peak_worker_and_cache_mb': n['peak_total_mb'],
                           'cpu_reservation_utilization': n['cpu_seconds'] / (max(makespan, 1e-9) * n['profile']['cpus'])}
                          for n in self.nodes]}


def percentile(values, q):
    return sorted(values)[min(len(values) - 1, math.ceil(len(values) * q) - 1)] if values else 0.


def experiments(profile, settings, seeds):
    validate(profile, settings)
    seeds = list(seeds)
    if not seeds:
        raise ValueError('At least one experiment seed is required')
    started = time.perf_counter()
    results = []
    for seed in seeds:
        workload = scenario(profile, seed, settings)
        digest = hashlib.sha256(json.dumps(workload, sort_keys=True).encode()).hexdigest()
        runs = [Simulation(profile, workload, settings, policy).run() for policy in ('round-robin', 'cache-aware')]
        results.append({'seed': seed, 'workload_sha256': digest, 'runs': runs,
                        'makespan_improvement_percent': 100 * (1 - runs[1]['makespan_seconds'] / max(runs[0]['makespan_seconds'], 1e-9))})
    elapsed = time.perf_counter() - started
    virtual = sum(run['virtual_seconds'] for result in results for run in result['runs'])
    failures = sum(bool(run['failures']) for result in results for run in result['runs'])
    return {'schema': SCHEMA, 'simulator_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            'python_version': sys.version.split()[0], 'calibration_sha256': hashlib.sha256(json.dumps(profile, sort_keys=True).encode()).hexdigest(),
            'calibration': {'captured_at': profile.get('captured_at'), 'source': profile.get('source'),
                            'nodes': [{key: value for key, value in node.items() if key != 'samples'} for node in profile['nodes']]},
            'settings': asdict(settings), 'experiments': results, 'summary': {
                'seeds': len(results), 'policy_runs': len(results) * 2, 'failed_runs': failures,
                'wall_seconds': elapsed, 'virtual_seconds': virtual, 'time_compression': virtual / max(elapsed, 1e-9),
                'median_makespan_improvement_percent': statistics.median(r['makespan_improvement_percent'] for r in results),
                'candidate_wins': sum(r['makespan_improvement_percent'] > 0 for r in results),
                'candidate_regressions': sum(r['makespan_improvement_percent'] < 0 for r in results),
                'worst_makespan_improvement_percent': min(r['makespan_improvement_percent'] for r in results),
                'worst_regression_seed': min(results, key=lambda r: r['makespan_improvement_percent'])['seed']},
            'limitations': [
                'Simulation of CPU jobs only; no GPU model and no production scheduler execution.',
                'Per-machine stage histories are empirical distributions, not matched hardware benchmarks; scheduling estimates use stage medians.',
                'Workloads balance stages uniformly and correlate per-machine sample quantiles; this is a synthetic stress distribution, not a replay of production arrival frequencies.',
                'Partial RAM peaks are lower bounds; memory_factor is a sensitivity assumption.',
                'CPU reservations are exclusive during modeled preparation/execution; production timesharing is not reproduced.',
                'Cache sizes, TTL, transfer rate, job arrivals/retries, admission pauses and background RAM are explicit synthetic assumptions.',
                'Idle entries expire or are evicted under pressure; active/preparing entries stay pinned. Quiescence must return cache RAM to zero.',
                'The capture is a paginated read of live journals, not a transactionally consistent fleet snapshot.']}


def write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, allow_nan=False) + '\n')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    capture = commands.add_parser('capture', help='Read all five authenticated node APIs; no mutations')
    capture.add_argument('--hosts', nargs=5)
    capture.add_argument('--controller', help='Use existing enrolled SSH access to read APIs from the signing controller')
    capture.add_argument('--max-pages', type=int, default=200)
    capture.add_argument('--output', required=True)
    suite_parser = commands.add_parser('suite', help='Stress matrix plus real production ownership checks')
    suite_parser.add_argument('--ownership-seeds', type=int, default=20)
    suite_parser.add_argument('--max-regression-percent', type=float)
    run = commands.add_parser('run', help='Offline, virtual-time prerelease experiments')
    for runner in (run, suite_parser):
        runner.add_argument('--profile', required=True)
        runner.add_argument('--shard-history', help='Aggregate completed shards from swarm_prerelease_shards')
        runner.add_argument('--output', required=True)
        runner.add_argument('--experiments', type=int, default=100)
        runner.add_argument('--seed', type=int, default=0)
        for name, field in Settings.__dataclass_fields__.items():
            if type(field.default) is not bool:
                runner.add_argument('--' + name.replace('_', '-'), type=type(field.default), default=field.default)
        runner.add_argument('--inject-cache-leak', action='store_true')
        runner.add_argument('--inject-global-guard', action='store_true')
    args = parser.parse_args(argv)
    if args.command == 'capture':
        if args.controller:
            sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'swarm-control/server'))
            from interface_metrics import capture as remote_capture
            # Only sanitized, bounded calibration samples are returned, never keys/config/paths.
            source = Path(__file__).read_text()
            script = ('from pathlib import Path\n'
                      "scope={'__name__':'swarm_calibration','__file__':str(Path.home()/'Documents/Organization/JSwarm/scripts/swarm_prerelease.py')}\n"
                      'import types,sys\nmodule=types.ModuleType(scope["__name__"]);module.__dict__.update(scope);sys.modules[module.__name__]=module\n'
                      f'exec({source!r},module.__dict__)\n'
                      f'data=module.capture_profiles({args.hosts!r},{args.max_pages!r})\n'
                      'import json,zlib,base64\nprint(json.dumps({"compressed":base64.b64encode(zlib.compress(json.dumps(data).encode())).decode()}))\n')
            result = remote_capture({'host': args.controller}, script, timeout=600)
            data = json.loads(zlib.decompress(base64.b64decode(result['compressed'])))
        else:
            data = capture_profiles(args.hosts, args.max_pages)
        write_json(args.output, data)
        print(json.dumps({'output': args.output, 'machines': [{k: n[k] for k in ('host', 'history')} for n in data['nodes']]}))
        return 0
    if args.experiments < 1:
        parser.error('--experiments must be positive')
    settings = Settings(**{name: getattr(args, name) for name in Settings.__dataclass_fields__ if name not in ('leak', 'global_guard')},
                        leak=args.inject_cache_leak, global_guard=args.inject_global_guard)
    profile = json.loads(Path(args.profile).read_text())
    if args.shard_history:
        profile['shard_history'] = json.loads(Path(args.shard_history).read_text())
    seeds = range(args.seed, args.seed + args.experiments)
    if args.command == 'suite':
        import importlib
        if not __package__:
            sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        checks = importlib.import_module((__package__ or 'scripts') + '.swarm_prerelease_checks')
        report = checks.suite(profile, settings, seeds, args.ownership_seeds, args.max_regression_percent)
    else:
        if args.shard_history:
            parser.error('--shard-history is supported by suite; run retains its synthetic workload')
        report = experiments(profile, settings, seeds)
    write_json(args.output, report)
    print(json.dumps(report['summary'], indent=2))
    return int(not report['summary']['passed']) if args.command == 'suite' else int(report['summary']['failed_runs'] > 0)


if __name__ == '__main__':
    raise SystemExit(main())
