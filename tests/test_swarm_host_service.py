import subprocess
from unittest.mock import Mock
from scripts import swarm_host_service as service


def test_owner_capsule_uses_host_bus_without_ssh_or_privilege(monkeypatch, tmp_path):
    monkeypatch.setattr(service.shutil, 'which', lambda _: None)
    monkeypatch.setattr(service.Path, 'home', lambda: tmp_path)
    monkeypatch.setattr(service.subprocess, 'check_output', lambda *a, **k: 'sha256:'+'a'*64)
    run = Mock(return_value=subprocess.CompletedProcess([], 0))
    monkeypatch.setattr(service.subprocess, 'run', run)
    service.systemctl('daemon-reload', check=True)
    args = run.call_args.args[0]
    assert args[:3] == ['docker', 'run', '--rm']
    assert '--privileged' not in args and 'ssh' not in args
    assert '--read-only' in args
    assert 'type=bind,src=/usr,dst=/usr,readonly' in args
    assert any(value.startswith('DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/') for value in args)
    assert args[-2:] == ['--user', 'daemon-reload']
    assert args[args.index('--pid')+1] == 'host'


def test_existing_host_fuse_policy_does_not_require_system_docker(monkeypatch):
    from scripts import swarm_host_service as service
    from unittest.mock import Mock
    monkeypatch.setattr(service.Path,'exists',lambda path:False)
    run=Mock(return_value=Mock(returncode=0))
    monkeypatch.setattr(service.subprocess,'run',run)
    service.configure_fuse_root_access()
    assert run.call_count==1
    command=run.call_args.args[0]
    assert '-H' not in command
    assert 'type=bind,src=/etc/fuse.conf,dst=/policy/fuse.conf,readonly' in command
