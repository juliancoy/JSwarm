from contextlib import nullcontext
from unittest.mock import Mock
import pytest
from scripts import deploy_swarm_inputs as deploy


def test_rollout_pins_published_artifact_and_passes_node_options(monkeypatch,tmp_path):
    monkeypatch.setattr(deploy,'RELEASES',tmp_path)
    monkeypatch.setattr(deploy,'deployment_lock',lambda **kw:nullcontext())
    check=Mock();monkeypatch.setattr(deploy,'check_publication',check)
    sweep=Mock(return_value=True);monkeypatch.setattr(deploy,'sweep',sweep)
    receipt={'published':str(tmp_path/'release.json'),'revision':'revision'}
    deploy.rollout_publication(receipt,['worker'],12,True)
    check.assert_called_once_with(tmp_path,'release.json')
    sweep.assert_called_once_with(['worker'],12,tmp_path/'release.json',parallel=True)


def test_superseded_publication_never_rolls_out(monkeypatch,tmp_path):
    monkeypatch.setattr(deploy,'RELEASES',tmp_path)
    monkeypatch.setattr(deploy,'deployment_lock',lambda **kw:nullcontext())
    monkeypatch.setattr(deploy,'check_publication',Mock(side_effect=RuntimeError('advanced')))
    sweep=Mock();monkeypatch.setattr(deploy,'sweep',sweep)
    with pytest.raises(RuntimeError,match='advanced'):
        deploy.rollout_publication({'published':str(tmp_path/'old'),'revision':'old'})
    sweep.assert_not_called()


def test_incomplete_rollout_exits_unsuccessfully(monkeypatch,tmp_path):
    monkeypatch.setattr(deploy,'RELEASES',tmp_path)
    monkeypatch.setattr(deploy,'deployment_lock',lambda **kw:nullcontext())
    monkeypatch.setattr(deploy,'check_publication',Mock())
    monkeypatch.setattr(deploy,'sweep',Mock(return_value=False))
    with pytest.raises(SystemExit) as stopped:
        deploy.rollout_publication({'published':str(tmp_path/'new'),'revision':'new'})
    assert stopped.value.code==1


def test_publish_combines_validation_and_pinned_rollout(monkeypatch,tmp_path):
    import json
    import subprocess
    monkeypatch.setattr(deploy,'ROOT',tmp_path/'JSwarm')
    monkeypatch.setattr(deploy,'RELEASES',tmp_path/'releases')
    monkeypatch.setattr(deploy,'publication_token',Mock(return_value='old'))
    def freeze(workspace,target):
        (target/'JSwarm').mkdir(parents=True)
        (target/'JSwarm/.node-git-source.json').write_text(json.dumps({'JSwarm':{'commit':'a'*40}}))
    monkeypatch.setattr(deploy,'freeze_source',freeze)
    receipt={'published':str(tmp_path/'releases/new'),'revision':'new'}
    def run(command,**kwargs):
        assert '--publish-only' in command
        path=command[command.index('--publication-receipt')+1]
        from pathlib import Path
        Path(path).write_text(json.dumps(receipt))
    monkeypatch.setattr(subprocess,'run',run)
    rollout=Mock();monkeypatch.setattr(deploy,'rollout_publication',rollout)
    monkeypatch.setattr(deploy.sys,'argv',['deploy','--publish','--node','worker','--wait-timeout','20','--parallel'])
    deploy.main()
    rollout.assert_called_once_with(receipt,['worker'],20,True)
    rollout.reset_mock()
    monkeypatch.setattr(deploy.sys,'argv',['deploy','--publish-only'])
    deploy.main()
    rollout.assert_not_called()
    def fail(*args,**kwargs):raise subprocess.CalledProcessError(1,['validation'])
    monkeypatch.setattr(subprocess,'run',fail)
    monkeypatch.setattr(deploy.sys,'argv',['deploy','--publish'])
    with pytest.raises(subprocess.CalledProcessError):deploy.main()
    rollout.assert_not_called()
