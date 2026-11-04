import sys
from types import SimpleNamespace
from unittest.mock import Mock
import pytest
from scripts.swarm_node import main


def test_control_snapshot_pins_split_owners_and_ignores_uncommitted_edits(tmp_path):
    import importlib.util
    from pathlib import Path
    import subprocess
    module=Path(__file__).resolve().parents[1]/'swarm-control/run.py'
    spec=importlib.util.spec_from_file_location('control_release',module)
    release=importlib.util.module_from_spec(spec);spec.loader.exec_module(release)
    root=tmp_path/'workspace';root.mkdir()
    def git(repo,*args):
        return subprocess.check_output(['git','-C',str(repo),*args],text=True,stderr=subprocess.DEVNULL).strip()
    def initialize(repo):
        repo.mkdir(exist_ok=True);git(repo,'init','-q')
        git(repo,'config','user.email','test@example.invalid');git(repo,'config','user.name','Test')
    initialize(root)
    (root/'.gitignore').write_text('JSwarm/\nGenomeHarbor/\n')
    for name in ('JSwarm','GenomeHarbor'):initialize(root/name)
    server=root/'JSwarm/swarm-control/server';(server/'view').mkdir(parents=True)
    (server/'server.py').write_text('reviewed server')
    (server/'view/genome.js').symlink_to('../../../../GenomeHarbor/web/genome.js')
    web=root/'GenomeHarbor/web';web.mkdir();asset=web/'genome.js';asset.write_text('reviewed genome')
    (root/'swarm-control').symlink_to('JSwarm/swarm-control')
    for repo in (root/'JSwarm',root/'GenomeHarbor',root):git(repo,'add','.');git(repo,'commit','-qm','reviewed')
    revision=git(root,'rev-parse','HEAD')
    source=release.release_snapshot(root,revision,tmp_path/'releases')
    assert (source/'server/server.py').read_text()=='reviewed server'
    assert (source/'server/view/genome.js').read_text()=='reviewed genome'
    asset.write_text('unreviewed change')
    assert release.release_snapshot(root,revision,tmp_path/'releases')==source
    assert (source/'server/view/genome.js').read_text()=='reviewed genome'
    git(root/'GenomeHarbor','commit','-qam','next reviewed genome')
    next_source=release.release_snapshot(root,revision,tmp_path/'releases')
    assert next_source!=source
    assert (next_source/'server/view/genome.js').read_text()=='unreviewed change'
    assert (source/'server/view/genome.js').read_text()=='reviewed genome'


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
