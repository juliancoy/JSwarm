"""Controller queue authority and typed RPC cleanup orchestration."""
import json
from pathlib import Path
from unittest.mock import Mock

import pytest

from JSwarm.scripts import swarm_cache_cleanup as cleanup

KEY = 'a' * 64


def entry(**changes):
    return {'key': KEY, 'container': 'swarm-job-' + KEY[:32], 'volume': 'swarm-cache-' + KEY[:32],
            'owner_id': 'b' * 64, 'volume_verified': True, 'owner_verified': True, 'consumers': [],
            'exec_readers': 0, 'created': '', **changes}


@pytest.fixture
def client():
    value = Mock()
    status = {'node': 'mini', 'machine': 'c' * 64, 'workers': [], 'capabilities': ['cache-cleanup-v1']}
    def call(operation, args=None, **kwargs):
        if operation == 'status':return status
        if operation == 'resource.capacity':return {'available_mb': 500}
        if operation == 'resource.caches.inspect':return {'items': [entry()]}
        if operation == 'resource.caches.cleanup':return {'removed': True, 'owner_removed': True}
        pytest.fail('Cleanup must never invoke a command, SSH, or prune operation: ' + operation)
    value.call.side_effect = call
    return value


@pytest.fixture
def policy():
    value = Mock()
    value._ownership_for.return_value = ({KEY}, {KEY: 1})
    value.finished_cache_keys.return_value = {KEY}
    return value


def test_default_preview_performs_no_mutation_rpc(client, policy):
    result = cleanup.clean_node('mini', client, policy, Path('/queue'))
    assert result['status'] == 'preview' and result['eligible_at_inspection'] == 1
    assert result['removed'] == []
    assert 'resource.caches.cleanup' not in [call.args[0] for call in client.call.call_args_list]


def test_apply_sends_one_exact_candidate_after_fresh_queue_check(client, policy):
    result = cleanup.clean_node('mini', client, policy, Path('/queue'), apply=True)
    assert result['status'] == 'complete' and len(result['removed']) == 1
    policy.finished_cache_keys.assert_called_once_with({'host': 'mini'}, Path('/queue'))
    request = next(call for call in client.call.call_args_list if call.args[0] == 'resource.caches.cleanup')
    assert request.args[1] == {'key': KEY, 'owner_id': 'b' * 64, 'machine': 'c' * 64,
                              'ownership': 'terminal-only', 'dry_run': False}


def test_new_unfinished_claim_blocks_the_apply_request(client, policy):
    policy.finished_cache_keys.return_value = set()
    result = cleanup.clean_node('mini', client, policy, Path('/queue'), apply=True)
    assert result['removed'] == [] and result['skipped'][0]['reason'] == 'ownership_changed_or_protected'
    assert 'resource.caches.cleanup' not in [call.args[0] for call in client.call.call_args_list]


@pytest.mark.parametrize('change', [{'volume_verified': False}, {'owner_verified': False},
                                     {'consumers': ['attached-job']}, {'exec_readers': 1}])
def test_ineligible_docker_objects_are_never_candidates(change):
    assert not cleanup.classify([entry(**change)], {KEY}, {})[0]['eligible']


def test_ownership_unknown_is_not_an_unused_cache():
    assert not cleanup.classify([entry()], set(), {})[0]['eligible']


def test_target_headroom_stops_without_removing_candidates(client, policy):
    result = cleanup.clean_node('mini', client, policy, Path('/queue'), apply=True, min_free_mb=400)
    assert result['removed'] == [] and result['stopped_reason'] == 'headroom_target_met'
    assert 'resource.caches.cleanup' not in [call.args[0] for call in client.call.call_args_list]


def test_older_node_is_reported_without_command_fallback(client, policy):
    client.call.side_effect = None
    client.call.return_value = {'node': 'mini', 'machine': 'c' * 64, 'workers': [], 'capabilities': ['node-command-v1']}
    result = cleanup.clean_node('mini', client, policy, Path('/queue'), apply=True)
    assert result['status'] == 'error' and 'standard update' in result['errors'][0]
    assert client.call.call_count == 1


def test_identity_change_before_apply_is_rejected(client, policy):
    original = client.call.side_effect;status_calls = []
    def call(op, args=None, **kwargs):
        result = original(op, args, **kwargs)
        if op == 'status':
            status_calls.append(True)
            result = {**result, 'machine': ('c' if len(status_calls) == 1 else 'd') * 64}
        return result
    client.call.side_effect = call
    result = cleanup.clean_node('mini', client, policy, Path('/queue'), apply=True)
    assert result['status'] == 'error' and not result['removed']
    assert 'resource.caches.cleanup' not in [call.args[0] for call in client.call.call_args_list]


def test_missing_controller_queue_refuses_all_node_operations(tmp_path, monkeypatch):
    import sys
    sys.path.insert(0, str(Path(cleanup.__file__).resolve().parents[1] / 'swarm-control/server'))
    import agent_client
    config = tmp_path / 'peers.json';config.write_text(json.dumps({'peers': {'mini': 'address'}}))
    monkeypatch.setattr(agent_client, 'CONFIG', config)
    constructor = Mock();monkeypatch.setattr(agent_client, 'AgentClient', constructor)
    with pytest.raises(ValueError, match='queue is missing'):
        cleanup.cleanup_fleet('', queue=tmp_path / 'missing', apply=True)
    constructor.assert_not_called()
