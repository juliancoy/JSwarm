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
        assert sorted(p.name for p in root.iterdir())==['Dockerfile','requirements-node.txt']
        assert 'zeroconf>=0.151,<1' in (root/'requirements-node.txt').read_text()
        dockerfile=(root/'Dockerfile').read_text()
        assert 'COPY scripts' not in dockerfile
        assert 'organization.node-runtime=' in dockerfile
        assert 'ENV PYTHONPATH=/opt/swarm-node' in dockerfile
        checked.append(image)
    monkeypatch.setattr(docker_utils,'build_image',build)
    assert build_node_image('swarm-node:test')=='swarm-node:test'
    assert checked==['swarm-node:test']


def test_recreation_reuses_matching_runtime_image(monkeypatch):
    from scripts.swarm_node import ensure_node_image,runtime_fingerprint
    from types import SimpleNamespace
    calls=[]
    def run(command,**kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=0,stdout=runtime_fingerprint()+'\n')
    monkeypatch.setattr('subprocess.run',run)
    assert ensure_node_image()=='organization-swarm-node:1'
    assert len(calls)==1
    assert calls[0][:3]==['docker','image','inspect']


def test_missing_or_obsolete_image_builds_through_build_script(monkeypatch):
    from scripts.swarm_node import ensure_node_image
    from types import SimpleNamespace
    calls=[]
    def run(command,**kwargs):
        calls.append((command,kwargs))
        return SimpleNamespace(returncode=0,stdout='old-code-image')
    monkeypatch.setattr('subprocess.run',run)
    ensure_node_image()
    assert calls[1][0]==['./build.sh']
    assert calls[1][1]['check'] is True


def test_runtime_fingerprint_ignores_code_but_tracks_dependency_changes(tmp_path,monkeypatch):
    from scripts import swarm_node
    project=tmp_path/'JSwarm';(project/'scripts').mkdir(parents=True)
    (project/'ramtop').mkdir()
    (project/'Dockerfile.swarm-node').write_text('FROM python:3.12\n')
    (project/'requirements-node.txt').write_text('docker>=7,<8\n')
    metadata=project/'ramtop/pyproject.toml'
    metadata.write_text('[project]\ndependencies=["rich>=13"]\n')
    monkeypatch.setattr(swarm_node,'__file__',str(project/'scripts/swarm_node.py'))
    before=swarm_node.runtime_fingerprint()
    (project/'scripts/launcher_agent.py').write_text('# new application code\n')
    assert swarm_node.runtime_fingerprint()==before
    metadata.write_text('[project]\ndependencies=["rich>=14"]\n')
    assert swarm_node.runtime_fingerprint()!=before
