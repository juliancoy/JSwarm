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


@pytest.mark.parametrize('healthy',[True,False])
def test_supervisor_drains_then_verifies_or_rolls_back(tmp_path,monkeypatch,healthy):
    """Exercise the actual loop without Docker, signals, or production markers."""
    import hashlib,json,time
    from pathlib import Path
    from scripts import launcher_peer_supervisor as supervisor
    base=tmp_path/'base';base.mkdir()
    config=tmp_path/'config/peers.json';config.parent.mkdir();config.write_text('{}')
    release=config.parent/'agent-releases'/hashlib.sha256(b'').hexdigest();release.mkdir(parents=True)
    names=('launcher_peers.py','launcher_discovery.py','launcher_registration.py','launcher_agent.py','launcher_registry.py')
    for name in names:(release/name).write_text('')
    (release/'manifest.json').write_text(json.dumps({n:hashlib.sha256(b'').hexdigest() for n in names}))
    current=release.parent/'current'
    def path(value):
        p=Path(value)
        return tmp_path/p.name if str(p).startswith('/tmp/launcher-agent-') else p
    path.home=Path.home
    busy=path('/tmp/launcher-agent-busy.json');busy.write_text('{"jobs":["accepted"]}')
    drain=path('/tmp/launcher-agent-draining')
    ready=path('/tmp/launcher-agent-ready.json')
    status=path('/tmp/launcher-agent-update.json')
    children=[];phases=[]
    class Child:
        def __init__(self,argv):
            self.folder=Path(argv[2]).parent;self.returncode=None
            children.append(self)
            ready.write_text(json.dumps({'folder':str(self.folder),'time':time.time()}))
            if self.folder==release and not healthy:self.returncode=1
        def poll(self):return self.returncode
        def terminate(self):self.returncode=0
        def wait(self,timeout=None):return self.returncode
    class Stop:
        ticks=0
        def is_set(self):return self.ticks>=6
        def wait(self,seconds):
            self.ticks+=1
            phases.append(json.loads(status.read_text())['phase'])
            if self.ticks==1:current.symlink_to(release.name)
            if self.ticks==2:
                assert drain.exists()
                assert len(children)==1 and children[0].poll() is None
            if self.ticks==3:
                busy.write_text('{"jobs":[]}')
                # A delayed old installer can race the replacement child's launch.
                drain.touch()
            if self.ticks==4:assert not drain.exists()
    monkeypatch.setattr(supervisor,'__file__',str(base/'supervisor.py'))
    monkeypatch.setattr(supervisor,'Path',path)
    monkeypatch.setenv('LIFETECH_PEER_CONFIG',str(config))
    monkeypatch.setattr(supervisor.sys,'argv',['supervisor'])
    monkeypatch.setattr(supervisor.signal,'signal',lambda *a:None)
    monkeypatch.setattr(supervisor.threading,'Event',Stop)
    monkeypatch.setattr(supervisor.subprocess,'Popen',Child)
    supervisor.main()
    assert phases[:3]==['ready','updating','starting']
    assert phases[-1]=='ready'
    assert len(children)==(2 if healthy else 3)
    assert children[-1].folder==(release if healthy else base)
    assert current.is_symlink() is healthy
    assert not drain.exists()
    assert bool(json.loads(status.read_text())['last_failure']) is not healthy


def test_release_cache_revalidates_changed_files(tmp_path,monkeypatch):
    from scripts import launcher_peer_supervisor as supervisor
    from unittest.mock import Mock
    folder=tmp_path/'release';folder.mkdir()
    names=('manifest.json','launcher_peers.py','launcher_discovery.py','launcher_registration.py','launcher_agent.py','launcher_registry.py')
    for name in names:(folder/name).write_text('original')
    verify=Mock(return_value=folder);monkeypatch.setattr(supervisor,'verify_release',verify)
    cache=supervisor.ReleaseCache()
    for _ in range(5):assert cache.verify(folder,tmp_path)==folder
    assert verify.call_count==1
    (folder/'launcher_agent.py').write_text('tampered')
    verify.side_effect=ValueError('checksum mismatch')
    with pytest.raises(ValueError,match='checksum'):cache.verify(folder,tmp_path)
    assert verify.call_count==2
