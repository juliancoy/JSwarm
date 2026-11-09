"""The fleet audit must stay read-only and preserve unknown observations."""
import json
import time
from unittest.mock import Mock

import pytest

from JSwarm.scripts import swarm_cache_audit as audit


@pytest.fixture
def observations():
    now = time.time()
    worker = {'active': True, 'group': 'shared-cache', 'memory_mb': 256,
              'memory_observation': {'sampled_at': now, 'raw_bytes': 128 * audit.MIB}}
    status = {'node': 'mini', 'machine': 'identity', 'time': now, 'ready': True,
              'worker_policy': {'accepting_jobs': True}, 'workers': [worker],
              'active_jobs': [], 'storage': {'retained_bytes': 100, 'workspace_budget_bytes': 1000}}
    capacity = {'available_mb': 500, 'memory_mb': 8000}
    inventory = {'caches': [{'container': 'one', 'image': 'image-1',
                             'files': {'object': {'bytes': 100 * audit.MIB, 'sha256': 'hash'}}}], 'pending_caches': []}
    return status, capacity, inventory


def test_reservations_samples_and_payload_are_separate(observations):
    status, capacity, inventory = observations
    client = Mock();client.call.side_effect = [status, capacity, inventory]
    row = audit.collect_node('mini', client)
    assert row['cache_reserved_mb'] == 256
    assert row['cache_sampled_raw_mb'] == 128
    assert row['verified_payload_mb'] == 100
    assert 'cache_pressure_without_other_workers' in row['warnings']
    assert [call.args[0] for call in client.call.call_args_list] == ['status', 'resource.capacity', 'resource.caches']


def test_stale_or_missing_samples_are_unknown_not_zero(observations):
    status, capacity, inventory = observations
    status['workers'][0]['memory_observation']['sampled_at'] -= 1000
    row = audit.summarize('mini', status, capacity, inventory, {}, time.time())
    assert row['cache_sampled_raw_mb'] is None
    assert row['cache_partial_sampled_raw_mb'] == 0
    assert row['cache_memory_samples'] == 0
    assert 'cache_memory_measurements_incomplete_or_stale' in row['warnings']


def test_inventory_failure_preserves_successful_node_observations(observations):
    status, capacity, _ = observations
    client = Mock();client.call.side_effect = [status, capacity, TimeoutError('inventory timeout')]
    row = audit.collect_node('mini', client)
    assert row['cache_workers'] == 1 and row['available_mb'] == 500
    assert row['verified_caches'] is None and row['verified_payload_mb'] is None
    assert 'resource.caches' in row['errors']


def test_wrong_identity_is_rejected_before_other_queries(observations):
    status, _, _ = observations
    client = Mock();client.call.return_value = {**status, 'node': 'other'}
    row = audit.collect_node('mini', client)
    assert 'status' in row['errors']
    assert 'cache_workers' not in row
    assert client.call.call_count == 1


def test_count_growth_requires_comparable_identity_and_older_snapshot(observations):
    status, capacity, inventory = observations
    before = audit.summarize('mini', status, capacity, inventory, {}, 100)
    current = {**before, 'observed_at': 200, 'cache_workers': 3, 'available_mb': 400, 'warnings': []}
    audit.compare({'nodes': [current]}, {'nodes': [before]})
    assert current['change']['cache_workers'] == 2
    assert current['change']['available_mb'] == -100
    assert 'cache_count_grew_while_headroom_fell' in current['warnings']
    changed = {**before, 'machine': 'replacement', 'observed_at': 200, 'warnings': []}
    audit.compare({'nodes': [changed]}, {'nodes': [before]})
    assert 'change' not in changed and 'comparison_unavailable' in changed


def test_identical_payload_across_images_is_reported_as_potential_duplication(observations):
    status, capacity, inventory = observations
    inventory['caches'].append({**inventory['caches'][0], 'image': 'image-2', 'container': 'two'})
    row = audit.summarize('mini', status, capacity, inventory, {}, time.time())
    duplicate = row['duplicate_payload_groups'][0]
    assert duplicate['copies'] == duplicate['images'] == 2
    assert duplicate['repeated_logical_payload_mb'] == 100
    assert row['verified_payload_mb'] == 200


def test_report_distinguishes_unavailable_and_partial(observations):
    status, capacity, inventory = observations
    good = audit.summarize('mini', status, capacity, inventory, {}, time.time())
    missing = {'node': 'offline', 'errors': {'status': 'offline'}, 'warnings': []}
    result = audit.report([{'sampled_at': time.time(), 'nodes': [good, missing]}])
    assert result['summary']['machines'] == 2
    assert result['summary']['status_unavailable'] == result['summary']['partial_machines'] == 1


def test_capture_covers_all_enrolled_peers_and_closes_every_client(tmp_path, monkeypatch, observations):
    import sys
    sys.path.insert(0, str(audit.Path(audit.__file__).resolve().parents[1] / 'swarm-control/server'))
    import agent_client
    config = tmp_path / 'peers.json'
    peers = {f'node-{i}': f'10.0.0.{i}' for i in range(7)}
    config.write_text(json.dumps({'peers': peers}))
    monkeypatch.setattr(agent_client, 'CONFIG', config)
    clients = {}
    status, capacity, inventory = observations
    def create(host):
        client = Mock()
        client.call.side_effect = [dict(status, node=host['host']), capacity, inventory]
        clients[host['host']] = client
        return client
    monkeypatch.setattr(agent_client, 'AgentClient', create)
    snapshot = audit.capture_snapshot()
    assert {row['node'] for row in snapshot['nodes']} == set(peers)
    assert len(clients) == 7
    for client in clients.values():
        client.close.assert_called_once()


def test_cli_writes_report_even_when_pressure_is_flagged(tmp_path, monkeypatch, observations):
    status, capacity, inventory = observations
    row = audit.summarize('mini', status, capacity, inventory, {}, time.time())
    monkeypatch.setattr(audit, 'capture_snapshot', lambda *args: {'sampled_at': time.time(), 'nodes': [row]})
    path = tmp_path / 'report.json'
    assert audit.main(['--output', str(path)]) == 1
    assert json.loads(path.read_text())['summary']['flagged_machines'] == 1
