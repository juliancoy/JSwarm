from types import SimpleNamespace
from unittest.mock import Mock
import run as runner


def test_existing_node_does_not_claim_readiness_when_portal_is_down(monkeypatch,capsys):
    command=Mock(return_value=SimpleNamespace(returncode=0,stdout='true'))
    monkeypatch.setattr(runner.subprocess,'run',command)
    monkeypatch.setattr(runner,'portal_ready',lambda port:False)
    assert runner.launch_container(SimpleNamespace(recreate_launcher=False,port=8443,login=None))==1
    assert 'not ready' in capsys.readouterr().out
    assert all(call.args[0][1]=='inspect' for call in command.call_args_list)


def test_existing_healthy_node_prints_portal_link_without_restarting(monkeypatch,capsys):
    command=Mock(return_value=SimpleNamespace(returncode=0,stdout='true'))
    monkeypatch.setattr(runner.subprocess,'run',command)
    monkeypatch.setattr(runner,'portal_ready',lambda port:True)
    assert runner.launch_container(SimpleNamespace(recreate_launcher=False,port=8443,login=None))==0
    assert 'https://localhost:8443/' in capsys.readouterr().out
    assert all(call.args[0][1]=='inspect' for call in command.call_args_list)
