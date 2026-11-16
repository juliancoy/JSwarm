import hashlib
import json
from pathlib import Path
from unittest.mock import Mock
import pytest
from agent_updates import LauncherUpdates


def release_source():
    source=LauncherUpdates(Path(__file__).parents[1],lambda:False,Mock())
    return source


def test_published_components_import_without_developer_workspace(tmp_path):
    import os
    import subprocess
    import sys
    from deployment_components import snapshot
    components = snapshot(Path(__file__).parents[1])
    for name, component in components.items():
        target = tmp_path/name;target.mkdir()
        for relative, source in component['files'].items():
            path = target/relative;path.parent.mkdir(parents=True, exist_ok=True);path.write_text(source)
        module = 'swarm_paged_cache' if name == 'public-pager' else 'deploy_swarm_filesystem'
        code = ("import sys;sys.path[:0]=[sys.argv[1],sys.argv[1]+'/swarm-control/server'];"
                "from scripts import "+module+" as component;"
                "assert component.__file__.startswith(sys.argv[1]+'/');"
                "from scripts.launcher_discovery import resolve_endpoint")
        subprocess.run([sys.executable, '-I', '-c', code, str(target)], cwd=target,
                       env={**os.environ, 'HOME': str(tmp_path)}, check=True, capture_output=True, text=True)


def test_publication_is_immutable_and_uses_existing_agent_revision(tmp_path):
    source=release_source()
    path=source.publish(tmp_path)
    loaded=LauncherUpdates.published(tmp_path/'desired',lambda:False,Mock())
    assert loaded.revision==source.revision
    assert loaded.files==source.files
    assert path==source.publish(tmp_path)
    assert 'durable-docker-workers-v1' in loaded.capabilities
    assert not any(word in path.read_text() for word in ('controller_private_key','controller_signing_key'))


def test_corrupt_publication_is_rejected(tmp_path):
    source=release_source()
    path=source.publish(tmp_path);value=json.loads(path.read_text())
    value['files']['launcher_agent.py']+='\n# changed\n'
    bad=tmp_path/'bad.json';bad.write_text(json.dumps(value))
    with pytest.raises(ValueError,match='checksum'):LauncherUpdates.published(bad,lambda:False,Mock())



def test_publication_retains_previous_and_idempotent_publish_preserves_it(tmp_path):
    source=release_source()
    first=source.publish(tmp_path)
    source.supervisor_source+='\n# next supervisor\n'
    second=source.publish(tmp_path)
    assert second!=first
    assert (tmp_path/'previous').resolve()==first
    source.publish(tmp_path)
    assert (tmp_path/'previous').resolve()==first
    restored=LauncherUpdates.rollback(tmp_path)
    assert restored.supervisor_source!=source.supervisor_source
    rolled_back=(tmp_path/'desired').resolve()
    assert rolled_back!=first
    assert restored.generation==3
    assert json.loads(rolled_back.read_text())['generation']==3
    assert (tmp_path/'previous').resolve()==second


def test_rollback_rejects_corrupt_previous_without_changing_desired(tmp_path):
    source=release_source()
    current=source.publish(tmp_path)
    bad=tmp_path/'bad.json';bad.write_text('{}')
    (tmp_path/'previous').symlink_to(bad.name)
    with pytest.raises((ValueError,KeyError)):LauncherUpdates.rollback(tmp_path)
    assert (tmp_path/'desired').resolve()==current


def test_pager_component_sources_are_pinned_and_tampering_is_rejected(tmp_path):
    source=release_source()
    path=source.publish(tmp_path)
    loaded=LauncherUpdates.published(path,lambda:False,Mock())
    assert loaded.components['public-pager']==source.components['public-pager']
    value=json.loads(path.read_text())
    value['components']['public-pager']['files']['scripts/swarm_paged_cache.py']+='\n# tampered\n'
    corrupt=tmp_path/'corrupt.json';corrupt.write_text(json.dumps(value))
    with pytest.raises(ValueError,match='component checksum'):LauncherUpdates.published(corrupt,lambda:False,Mock())


def test_source_snapshot_uses_commits_without_dirty_or_untracked_runtime_data(tmp_path):
    import subprocess
    from scripts.deploy_swarm_inputs import freeze_source
    root=tmp_path/'Organization';project=root/'JSwarm'
    for repo in (root,project,root/'HarborGenetics'):
        repo.mkdir(parents=True,exist_ok=True)
        subprocess.run(['git','init','-q',str(repo)],check=True)
        subprocess.run(['git','-C',str(repo),'config','user.name','Test'],check=True)
        subprocess.run(['git','-C',str(repo),'config','user.email','test@localhost'],check=True)
        subprocess.run(['git','-C',str(repo),'remote','add','origin',str(repo)],check=True)
        (repo/'README').write_text('committed source')
        subprocess.run(['git','-C',str(repo),'add','README'],check=True)
        subprocess.run(['git','-C',str(repo),'commit','-qm','source'],check=True)
    (project/'README').write_text('dirty edit')
    (project/'runtime-private').write_text('excluded')
    (root/'runtime-private').write_text('excluded')
    target=tmp_path/'snapshot';freeze_source(root,target)
    assert (target/'JSwarm/README').read_text()=='committed source'
    assert not (target/'JSwarm/runtime-private').exists()
    assert not (target/'runtime-private').exists()



def test_git_provenance_is_covered_by_publication_integrity(tmp_path):
    source=release_source()
    path=source.publish(tmp_path)
    assert LauncherUpdates.published(path,lambda:False,Mock()).git_source==source.git_source
    value=json.loads(path.read_text());value['git_source']['repositories']['JSwarm']['commit']='b'*40
    corrupt=tmp_path/'corrupt.json';corrupt.write_text(json.dumps(value))
    with pytest.raises(ValueError,match='checksum mismatch'):
        LauncherUpdates.published(corrupt,lambda:False,Mock())


def test_publication_and_staging_reject_missing_git_provenance(tmp_path):
    source=release_source();source.git_source=None
    with pytest.raises(ValueError,match='Git source provenance required'):source.publish(tmp_path)
    with pytest.raises(ValueError,match='Git source provenance required'):source.stage({}, {})
    assert not (tmp_path/'desired').exists()


def test_unsupported_publication_is_never_loaded_or_offered_for_rollback(tmp_path):
    source=release_source()
    path=tmp_path/'00000000000000000052-old.json'
    path.write_text(json.dumps({'schema':'retired-format','untrusted_code':'never execute'}))
    (tmp_path/'desired').symlink_to(path.name)
    with pytest.raises(ValueError,match='Unsupported published release schema'):
        LauncherUpdates.published(path,lambda:False,Mock())
    updated=source.publish(tmp_path)
    assert updated!=path and source.generation==53
    assert not (tmp_path/'previous').exists()
    assert path.exists()  # Opaque receipts remain available for audit.
    assert (tmp_path/'desired').resolve()==updated
