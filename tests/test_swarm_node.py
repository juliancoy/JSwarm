import sys
from types import SimpleNamespace
from unittest.mock import Mock
import pytest
from scripts.swarm_node import main


def test_swarm_node_routes_runtime_to_single_supervisor(monkeypatch):
    called=Mock(side_effect=lambda:list(sys.argv))
    monkeypatch.setitem(sys.modules,'launcher_peer_supervisor',SimpleNamespace(main=called))
    assert main(['run','--prefix','node-'])==['swarm-node','--prefix','node-']
    called.assert_called_once()


def test_swarm_node_exposes_existing_fleet_maintenance(monkeypatch):
    called=Mock(side_effect=lambda:list(sys.argv))
    monkeypatch.setitem(sys.modules,'worker_maintenance',SimpleNamespace(main=called))
    assert main(['drain','--node','machine.local','--reason','upgrade'])==['swarm-node','drain','--reason','upgrade','--node','machine.local']


def test_swarm_node_update_uses_standard_deployer(monkeypatch):
    called=Mock(side_effect=lambda:list(sys.argv))
    monkeypatch.setitem(sys.modules,'deploy_swarm_inputs',SimpleNamespace(main=called))
    assert main(['update','--watch'])==['swarm-node','--watch']


def test_swarm_node_enrollment_requires_explicit_target():
    with pytest.raises(SystemExit):main(['enroll'])


def test_node_image_contains_shared_sdk_and_discovery_dependencies(monkeypatch):
    from pathlib import Path
    from scripts.swarm_node import build_node_image
    import docker_utils
    checked=[]
    def build(image,folder):
        root=Path(folder)
        for name in ('scripts/swarm_runtime.py','scripts/genome_runtime.py','docker_utils/policy.py','docker_utils/images.py','swarm-control/server/agent_client.py'):
            assert (root/name).is_file(),name
        dockerfile=(root/'Dockerfile').read_text()
        assert 'COPY docker_utils /opt/swarm-node/docker_utils' in dockerfile
        assert 'ENV PYTHONPATH=/opt/swarm-node' in dockerfile
        checked.append(image)
    monkeypatch.setattr(docker_utils,'build_image',build)
    assert build_node_image('swarm-node:test')=='swarm-node:test'
    assert checked==['swarm-node:test']
