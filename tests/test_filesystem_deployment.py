import hashlib
import io
import json
import os
from pathlib import Path
import sys
import pytest
from scripts.deploy_swarm_filesystem import INSTALL


def test_disabled_fuse_configures_api_without_installing_mount_dependencies(tmp_path, monkeypatch, capsys):
    (tmp_path/'.config/lifetech-launcher').mkdir(parents=True)
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    payload = {'machine': hashlib.sha256(Path('/etc/machine-id').read_bytes()).hexdigest(),
               'settings': {'namespace': 'test', 'fuse': False},
               'files': {'scripts/swarm_host_service.py': "raise AssertionError('No mount service should be started')"}}
    monkeypatch.setattr(sys, 'stdin', io.StringIO(json.dumps(payload)))
    previous = os.umask(0o077)
    try:
        with pytest.raises(SystemExit) as error:exec(INSTALL, {})
        assert error.value.code == 0
    finally:os.umask(previous)
    result = json.loads(capsys.readouterr().out)
    assert result['fuse'] is False and result['active'] is True
    assert not (tmp_path/'.local/share/swarm-filesystem/dependencies').exists()
    assert not (tmp_path/'SwarmDNA').exists()
    assert json.loads((tmp_path/'.config/lifetech-launcher/filesystem.json').read_text())['fuse'] is False


def test_ssh_owner_command_uses_enrolled_mdns_and_literal_code():
    from scripts.deploy_swarm_filesystem import ssh_command
    import shlex
    enrolled={'name':'compai.local','peers':{'compai.local':{},'codecollective.local':{}}}
    plan={'ssh_users':{'codecollective.local':'codecollective'}}
    code="print('literal $HOME `uname`')"
    args=ssh_command('codecollective.local',code,plan,enrolled)
    assert args[-2]=='codecollective@codecollective.local'
    assert shlex.split(args[-1])==['python3','-c',code]
    with pytest.raises(ValueError):ssh_command('unknown.local',code,plan,enrolled)


def test_standard_snapshot_contains_every_filesystem_runtime_dependency(tmp_path):
    from deployment_components import snapshot,deploy
    from scripts.deploy_swarm_filesystem import FILES
    root=Path(__file__).resolve().parents[1]
    component=snapshot(root)['private-filesystem']
    assert set(FILES)<=set(component['files'])
    # Import/run the frozen component with no private plan, so deployment stays
    # local and does not contact fleet nodes or use the developer checkout.
    import subprocess
    from unittest.mock import patch
    with patch.dict(os.environ,{'HOME':str(tmp_path)}):
        assert deploy(component,[])=={}
