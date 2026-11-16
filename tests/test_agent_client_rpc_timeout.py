import importlib.util
import json
from pathlib import Path
from unittest.mock import Mock


def test_address_fallback_timeout_does_not_shorten_rpc(monkeypatch, tmp_path):
    spec = importlib.util.spec_from_file_location('rpc_timeout_client', Path(__file__).parents[1]/'swarm-control/server/agent_client.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    config = tmp_path/'peers.json'
    config.write_text(json.dumps({'name':'worker.local','peers':['worker.local'],'key':'00'*32}))
    monkeypatch.setattr(module,'CONFIG',config)
    from scripts import launcher_discovery
    monkeypatch.setattr(launcher_discovery,'resolve_service',lambda name:(None,['fe80::1%eth0','192.0.2.1']))
    sock = Mock()
    connect = Mock(return_value=sock)
    monkeypatch.setattr(module,'multipath_connection',connect)
    client = module.AgentClient({'host':'worker.local','agent_endpoint':'http://worker.local:8790'})
    assert client.connection._create_connection(('worker.local',8790),120) is sock
    assert connect.call_args.args[1] == 2
    sock.settimeout.assert_called_once_with(120)
