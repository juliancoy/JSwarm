from scripts.launcher_peer_supervisor import prepare_update
import pytest


def test_busy_worker_is_replaced_without_waiting_for_work(tmp_path):
    marker=tmp_path/'draining'
    assert prepare_update(lambda:False,marker) is False
    assert marker.exists()
    assert prepare_update(lambda:False,marker) is True
    assert marker.exists()


def test_update_gates_new_admission_for_one_tick(tmp_path):
    marker=tmp_path/'draining'
    assert prepare_update(lambda:True,marker) is False
    assert marker.exists()
    assert prepare_update(lambda:True,marker) is True


def test_update_gate_preserves_owner_policy_and_maintenance(tmp_path):
    import json
    from scripts.launcher_agent import worker_policy
    config=tmp_path/'peers.json';drain=tmp_path/'draining'
    policy=tmp_path/'worker-policy.json';policy.write_text(json.dumps({'mode':'always'}))
    maintenance=tmp_path/'maintenance.json';maintenance.write_text('{}')
    drain.touch()
    assert worker_policy(config,drain_path=drain)['mode']=='draining'
    drain.unlink()
    assert worker_policy(config,drain_path=drain)['mode']=='maintenance'
    maintenance.unlink()
    assert worker_policy(config,drain_path=drain)['accepting_jobs'] is True
    assert json.loads(policy.read_text())=={'mode':'always'}


def test_maintenance_blocks_always_policy_without_replacing_owner_preference(tmp_path):
    import json
    from scripts.launcher_agent import worker_policy
    config=tmp_path/'peers.json'
    policy=tmp_path/'worker-policy.json';policy.write_text(json.dumps({'mode':'always'}))
    (tmp_path/'maintenance.json').write_text('{}')
    assert worker_policy(config)['accepting_jobs'] is False
    assert worker_policy(config)['mode']=='maintenance'
    (tmp_path/'maintenance.json').unlink()
    assert worker_policy(config)['accepting_jobs'] is True
    assert json.loads(policy.read_text())=={'mode':'always'}


def test_hdd_startup_budget_is_explicit_bounded_and_does_not_disable_rollback(tmp_path):
    import json
    from scripts.launcher_peer_supervisor import startup_timeout
    config=tmp_path/'peers.json'
    assert startup_timeout(config)==180
    for value,expected in [(900,900),(9999,900),(1,45),(-1,180),(True,180),('forever',180)]:
        config.write_text(json.dumps({'agent_startup_timeout_seconds':value}))
        assert startup_timeout(config)==expected



def test_release_cache_revalidates_changed_files(tmp_path,monkeypatch):
    from scripts import launcher_peer_supervisor as supervisor
    from unittest.mock import Mock
    folder=tmp_path/'release';folder.mkdir()
    names=('source.json',)
    for name in names:(folder/name).write_text('original')
    verify=Mock(return_value=folder);monkeypatch.setattr(supervisor,'verify_release',verify)
    cache=supervisor.ReleaseCache()
    for _ in range(5):assert cache.verify(folder,tmp_path)==folder
    assert verify.call_count==1
    (folder/'source.json').write_text('tampered')
    verify.side_effect=ValueError('checksum mismatch')
    with pytest.raises(ValueError,match='checksum'):cache.verify(folder,tmp_path)
    assert verify.call_count==2


def test_copied_agent_release_is_rejected(tmp_path):
    from scripts.launcher_peer_supervisor import verify_release
    release=tmp_path/'copied';release.mkdir()
    (release/'manifest.json').write_text('{}')
    with pytest.raises(FileNotFoundError):verify_release(release,tmp_path)
