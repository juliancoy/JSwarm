"""Fast prerelease model regressions; never contact the fleet or start containers."""
from copy import deepcopy
from dataclasses import replace
import json

import pytest

from JSwarm.scripts import swarm_prerelease as sim


@pytest.fixture
def profile():
    return {'schema': 1, 'nodes': [
        {'host': f'node-{index}', 'cpus': 4, 'memory_mb': 2048,
         'samples': [{'family': 'test/worker', 'seconds': 10 + index * 10, 'cpus': 1,
                      'memory_mb': 64, 'memory_coverage': 'final-pre-exit',
                      'cpu_coverage': 'final-pre-exit', 'cpu_seconds': 4}]}
        for index in range(5)]}


def row(**updates):
    return {'job': 'a', 'run': 'extraction', 'group': 'tests', 'exit_code': 0,
            'elapsed_seconds': 20, 'cpus': 2, 'memory_peak_bytes': 64 * sim.MIB,
            'memory_accounting_coverage': 'final-pre-exit', **updates}


def test_limits_and_partial_cpu_are_not_treated_as_complete_measurements():
    assert sim.measurement(row(memory_peak_bytes=None, memory_mb=65536)) is None
    assert sim.measurement(row(exit_code=137)) is None
    assert sim.measurement(row(sink='gpu')) is None
    sample = sim.measurement(row(memory_accounting_coverage='sampled-partial',
                                 cpu_accounting_coverage='sampled-partial', cpu_core_seconds=3))
    assert sample['memory_mb'] == 64
    assert sample['memory_coverage'] == sample['cpu_coverage'] == 'sampled-partial'
    assert sample['seconds'] == 20


def test_calibration_paginates_and_refuses_truncated_history():
    class Client:
        def __init__(self):
            self.cursors = []

        def call(self, op, args=None, **kwargs):
            if op == 'resource.capacity':
                return {'cpus': 4, 'memory_mb': 2048}
            self.cursors.append(args['after'])
            return {'items': [row(job='b' if args['after'] else 'a')],
                    'next': None if args['after'] else 'a'}

    client = Client()
    result = sim.calibrate('node', client)
    assert client.cursors == ['', 'a']
    assert result['history']['usable'] == 2
    assert 'job' not in json.dumps(result['samples'])
    with pytest.raises(ValueError, match='increase --max-pages'):
        sim.calibrate('node', Client(), max_pages=1)


def test_no_measured_history_cannot_silently_become_generic_machine():
    class Client:
        def call(self, op, *args, **kwargs):
            return {'cpus': 4, 'memory_mb': 2048} if op == 'resource.capacity' else {'items': [row(memory_peak_bytes=None)], 'next': None}

    with pytest.raises(ValueError, match='refusing invented calibration'):
        sim.calibrate('node', Client())


def test_seed_replay_and_policies_share_identical_workload(profile):
    settings = sim.Settings(jobs=80, cache_mb=32)
    before = deepcopy(profile)
    first = sim.experiments(profile, settings, [13, 14])
    second = sim.experiments(profile, settings, [13, 14])
    assert first['experiments'] == second['experiments']
    assert first['experiments'][0]['workload_sha256'] != first['experiments'][1]['workload_sha256']
    assert profile == before
    assert first['summary']['failed_runs'] == 0
    assert all(run['completed'] == 80 for trial in first['experiments'] for run in trial['runs'])
    assert all(node['final_cache_mb'] == 0 for trial in first['experiments'] for run in trial['runs'] for node in run['nodes'])


def test_many_waves_do_not_accumulate_caches_and_all_five_nodes_work(profile):
    settings = sim.Settings(jobs=400, waves=20, cache_mb=64, cache_ttl=30)
    result = sim.experiments(profile, settings, [5])
    assert result['summary']['failed_runs'] == 0
    for run in result['experiments'][0]['runs']:
        assert all(node['completed'] for node in run['nodes'])
        assert all(node['peak_worker_and_cache_mb'] <= 2048 * .8 for node in run['nodes'])
        assert run['stats']['events'] < settings.jobs * 5 + 20
        assert run['stats']['retries'] > 0


def test_cache_leak_negative_control_is_detected(profile):
    settings = sim.Settings(jobs=300, waves=15, cache_mb=256, leak=True)
    report = sim.experiments(profile, settings, [2])
    assert report['summary']['failed_runs'] == 2
    kinds = {f['kind'] for trial in report['experiments'] for run in trial['runs'] for f in run['failures']}
    assert 'cache_accumulation' in kinds
    assert 'admission_stall_or_unplaceable' in kinds


def test_global_preparation_guard_negative_control_stalls_under_pressure(profile):
    settings = sim.Settings(jobs=200, waves=4, cache_mb=256, global_guard=True)
    report = sim.experiments(profile, settings, [2])
    assert report['summary']['failed_runs'] > 0
    assert any(f['kind'] == 'admission_stall_or_unplaceable'
               for trial in report['experiments'] for run in trial['runs'] for f in run['failures'])


def test_cache_loading_is_shared_and_pinned_until_last_consumer(profile):
    # Eligibility confines this stage to one node, forcing concurrent reuse.
    for node in profile['nodes'][1:]:
        node['samples'][0]['family'] = 'other/worker'
    job = {'id': 0, 'arrival': 0, 'family': 'test/worker', 'quantile': .5,
           'scale': 1, 'dataset': 'shared', 'cache_mb': 32, 'retry': False}
    workload = {'jobs': [job, {**job, 'id': 1}], 'pauses': {n['host']: (0, 0) for n in profile['nodes']}}
    result = sim.Simulation(profile, workload, sim.Settings(cache_ttl=0), 'cache-aware').run()
    assert result['completed'] == 2
    assert result['stats']['cache_loads'] == 1
    assert result['stats']['cache_hits'] == 1
    assert result['makespan_seconds'] == pytest.approx(10.32)
    assert result['failures'] == []


def test_unplaceable_work_is_reported_without_spinning(profile):
    settings = sim.Settings(jobs=10, cache_mb=1e6)
    report = sim.experiments(profile, settings, [0])
    assert report['summary']['failed_runs'] == 2
    assert all(run['stats']['events'] <= 20 for run in report['experiments'][0]['runs'])


def test_virtual_clock_advances_days_without_sleep(profile, monkeypatch):
    def forbidden_sleep(*args):
        raise AssertionError('Virtual-time simulation must not sleep')
    monkeypatch.setattr(sim.time, 'sleep', forbidden_sleep)
    for node in profile['nodes']:
        node['samples'][0]['seconds'] *= 86400
    report = sim.experiments(profile, sim.Settings(jobs=10), [1])
    assert report['summary']['virtual_seconds'] > 86400
    assert report['summary']['failed_runs'] == 0


def test_cli_writes_report_and_returns_nonzero_for_gate_failure(profile, tmp_path):
    calibration = tmp_path / 'profile.json'
    output = tmp_path / 'report.json'
    sim.write_json(calibration, profile)
    args = ['run', '--profile', str(calibration), '--output', str(output), '--experiments', '2', '--jobs', '40', '--cache-mb', '32']
    assert sim.main(args) == 0
    assert json.loads(output.read_text())['summary']['policy_runs'] == 4
    assert sim.main(args + ['--inject-cache-leak']) == 1


def test_invalid_profiles_and_settings_fail_explicitly(profile):
    with pytest.raises(ValueError, match='five nodes'):
        sim.validate({**profile, 'nodes': profile['nodes'][:4]}, sim.Settings())
    with pytest.raises(ValueError, match='transfer_mb_s'):
        sim.validate(profile, replace(sim.Settings(), transfer_mb_s=0))


def explicit_workload(profile, jobs, restarts=()):
    return {'jobs': jobs, 'pauses': {node['host']: (0, 0) for node in profile['nodes']}, 'restarts': restarts}


def single_node_jobs(profile):
    for node in profile['nodes'][1:]:
        node['samples'][0]['family'] = 'other/worker'
    return {'id': 0, 'arrival': 0, 'family': 'test/worker', 'quantile': .5,
            'scale': 1, 'dataset': 'shared', 'cache_mb': 32, 'retry': False, 'depends_on': []}


def test_restart_releases_claims_retries_and_ignores_stale_completion(profile):
    job = single_node_jobs(profile)
    workload = explicit_workload(profile, [job], [{'node': 0, 'at': 1, 'duration': 2}])
    run = sim.Simulation(profile, workload, sim.Settings(), 'cache-aware').run()
    assert run['completed'] == 1 and run['cancelled'] == 0
    assert run['stats']['restart_retries'] == run['stats']['stale_completions_ignored'] == 1
    assert run['stats']['node_recoveries'] == 1
    assert run['makespan_seconds'] == pytest.approx(13.32)
    assert run['nodes'][0]['cpu_reservation_utilization'] <= 1
    assert run['failures'] == []


def test_cancelling_one_preparing_consumer_preserves_the_other(profile):
    job = single_node_jobs(profile)
    workload = explicit_workload(profile, [{**job, 'cancel_at': .1}, {**job, 'id': 1}])
    run = sim.Simulation(profile, workload, sim.Settings(), 'round-robin').run()
    assert run['completed'] == run['cancelled'] == 1
    assert run['stats']['cache_loads'] == 1
    assert run['stats']['cache_hits'] == 1
    assert run['failures'] == []


def test_cancelled_preparation_drops_unfinished_load(profile):
    job = single_node_jobs(profile)
    workload = explicit_workload(profile, [{**job, 'cancel_at': .1}])
    run = sim.Simulation(profile, workload, sim.Settings(), 'round-robin').run()
    assert run['cancelled'] == 1 and run['completed'] == 0
    assert run['stats']['abandoned_cache_loads'] == 1
    assert run['failures'] == []


def test_dependencies_wait_for_successful_retry(profile):
    job = single_node_jobs(profile)
    workload = explicit_workload(profile, [{**job, 'retry': True}, {**job, 'id': 1, 'depends_on': [0]}])
    run = sim.Simulation(profile, workload, sim.Settings(), 'round-robin').run()
    assert run['completed'] == 2
    assert run['stats']['retries'] == 1
    assert run['makespan_seconds'] == pytest.approx(30.32)
    assert run['failures'] == []


def test_parent_cancellation_propagates_to_future_descendants(profile):
    job = single_node_jobs(profile)
    workload = explicit_workload(profile, [{**job, 'cancel_at': 1},
        {**job, 'id': 1, 'arrival': 2, 'depends_on': [0]},
        {**job, 'id': 2, 'arrival': 3, 'depends_on': [1]}])
    run = sim.Simulation(profile, workload, sim.Settings(), 'round-robin').run()
    assert run['cancelled'] == 3 and run['completed'] == 0
    assert run['stats']['dependency_cancellations'] == 2
    assert run['failures'] == []


def test_production_ownership_audit_and_fault_detection():
    from JSwarm.scripts.swarm_prerelease_checks import queue_policy_audit
    audit = queue_policy_audit(range(8))
    assert audit['checks'] == 240
    assert audit['failures'] == []
    faulty = queue_policy_audit([0], policy=lambda *args: set())
    assert any(f['case'] == 'legacy_other_node' for f in faulty['failures'])


def test_stress_suite_covers_recovery_and_reports_regression_seeds(profile):
    from JSwarm.scripts.swarm_prerelease_checks import suite
    report = suite(profile, sim.Settings(jobs=35, cache_mb=8), [7], ownership_seeds=1)
    assert report['summary']['passed']
    assert report['summary']['scenarios'] == 6
    assert report['summary']['policy_runs'] == 12
    for run in report['scenarios']['failure-recovery']['experiments'][0]['runs']:
        assert run['stats']['node_restarts'] == run['stats']['node_recoveries'] == 5
        assert run['completed'] + run['cancelled'] == run['submitted']
    assert all('worst_regression_seed' in r['summary'] for r in report['scenarios'].values())


def test_completed_shard_summary_deduplicates_and_scopes_cache_inputs(tmp_path):
    import sqlite3
    from JSwarm.scripts.swarm_prerelease_shards import summarize
    key = 'a' * 64
    binding = {'host': 'node-0', 'key': key, 'volume': 'swarm-cache-' + key[:32], 'memory_mb': 77}
    request = {'worker_host': 'node-0', 'directory': str(tmp_path), 'cache_inputs': [binding],
               'transport': {'references': [binding, {**binding, 'host': 'node-1', 'memory_mb': 1000}]}}
    dbpath = tmp_path / 'queue.sqlite'
    with sqlite3.connect(dbpath) as db:
        db.execute('CREATE TABLE submissions(id TEXT,host TEXT,request TEXT,result TEXT,state TEXT,namespace TEXT)')
        db.execute('INSERT INTO submissions VALUES(?,?,?,?,?,?)', ('private-specimen-id', 'node-0', json.dumps(request),
                   json.dumps({'method': 'calling'}), 'completed', 'genomics-followup:fixture'))
    manifest = tmp_path / 'plan.json'
    sim.write_json(manifest, {'jobs': [{'id': 'private-specimen-id', 'depends_on': ['one', 'two']}]})
    sim.write_json(tmp_path / 'execution.json', {'elapsed_seconds': 15})
    summary = summarize(dbpath, manifest)
    assert summary['worker_cache_budget_mb_by_method'] == {'calling': [77]}
    assert summary['worker_cache_bindings_per_job'] == [1]
    assert summary['dependency_fan_in_by_method'] == {'calling': [2]}
    assert summary['execution_seconds_by_method'] == {'calling': [15]}
    encoded = json.dumps(summary)
    assert 'private-specimen-id' not in encoded and key not in encoded and str(tmp_path) not in encoded


def test_shard_inspired_suite_exposes_unmodeled_stages(profile):
    from JSwarm.scripts.swarm_prerelease_checks import suite
    for node in profile['nodes']:
        node['samples'][0]['family'] = 'test/call'
    profile['shard_history'] = {'completed_stages': {'calling': 10, 'phase': 2},
        'worker_cache_budget_mb_by_method': {'calling': [8, 16]},
        'dependency_fan_in_by_method': {'calling': [0, 1, 2]}}
    report = suite(profile, sim.Settings(jobs=20, cache_mb=8), [0], ownership_seeds=1)
    assert report['summary']['passed']
    assert report['summary']['scenarios'] == 7
    assert report['completed_shard_coverage']['modeled_methods'] == ['calling']
    assert report['completed_shard_coverage']['unmodeled_methods'] == ['phase']
    assert report['scenarios']['completed-shards']['summary']['failed_runs'] == 0


def test_cancellation_after_completion_cannot_erase_result(profile):
    job = single_node_jobs(profile)
    run = sim.Simulation(profile, explicit_workload(profile, [{**job, 'cancel_at': 20}]),
                         sim.Settings(), 'cache-aware').run()
    assert run['completed'] == 1 and run['cancelled'] == 0
    assert run['failures'] == []
