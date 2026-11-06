import hashlib
import json
from pathlib import Path
from unittest.mock import Mock
import pytest
from agent_updates import LauncherUpdates
from scripts import launcher_update


def release_source():
    source=LauncherUpdates(Path(__file__).parents[1],lambda:False,Mock())
    source.git_source={'repositories':{name:{'origin':'https://example.com/'+name+'.git','commit':'a'*40}
                       for name in ('Organization','JSwarm','GenomeHarbor')},'installer':'# test Git installer\n'}
    return source


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


def test_corrupt_installed_release_cannot_be_activated(tmp_path,monkeypatch):
    config=tmp_path/'config/peers.json';config.parent.mkdir();config.write_text('{}')
    sources=tmp_path/'sources';sources.mkdir()
    for name in launcher_update.FILES:(sources/name).write_text('# source\n')
    revision=hashlib.sha256(b''.join((sources/name).read_bytes() for name in launcher_update.FILES)).hexdigest()
    release=config.parent/'agent-releases'/revision;release.mkdir(parents=True)
    for name in launcher_update.FILES:(release/name).write_text('# tampered\n')
    (release/'manifest.json').write_text(json.dumps({n:hashlib.sha256(b'# source\n').hexdigest() for n in launcher_update.FILES}))
    monkeypatch.setenv('LIFETECH_PEER_CONFIG',str(config));monkeypatch.setattr(launcher_update,'__file__',str(sources/'launcher_update.py'))
    monkeypatch.setattr('sys.argv',['launcher_update.py'])
    with pytest.raises(ValueError,match='checksum'):launcher_update.main()
    assert not (release.parent/'current').exists()


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
    for repo in (root,project,root/'GenomeHarbor'):
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


def test_local_update_bundles_stream_runtime_in_installed_agent(tmp_path,monkeypatch):
    sources=tmp_path/'sources';sources.mkdir()
    config=tmp_path/'config/peers.json';config.parent.mkdir();config.write_text('{}')
    for name in launcher_update.FILES:(sources/name).write_text('# source\n')
    (sources/'launcher_streams.py').write_text('VERSION="bundled-test"\n')
    (sources/'launcher_commands.py').write_text('VERSION="command-test"\n')
    monkeypatch.setenv('LIFETECH_PEER_CONFIG',str(config))
    monkeypatch.setattr(launcher_update,'__file__',str(sources/'launcher_update.py'))
    monkeypatch.setattr('sys.argv',['launcher_update.py'])
    launcher_update.main()
    installed=(config.parent/'agent-releases/current').resolve()
    namespace={}
    exec((installed/'launcher_agent.py').read_text(),namespace)
    assert namespace['STREAM_RUNTIME_SOURCE']==(sources/'launcher_streams.py').read_text()

    assert namespace['COMMAND_RUNTIME_SOURCE']==(sources/'launcher_commands.py').read_text()


def test_git_provenance_is_covered_by_publication_integrity(tmp_path):
    source=release_source()
    source.git_source={'repositories':{name:{'origin':'https://example.com/'+name+'.git','commit':'a'*40}
                       for name in ('Organization','JSwarm','GenomeHarbor')},'installer':'# verified installer\n'}
    path=source.publish(tmp_path)
    assert LauncherUpdates.published(path,lambda:False,Mock()).git_source==source.git_source
    value=json.loads(path.read_text());value['git_source']['repositories']['JSwarm']['commit']='b'*40
    corrupt=tmp_path/'corrupt.json';corrupt.write_text(json.dumps(value))
    with pytest.raises(ValueError,match='artifact version history'):
        LauncherUpdates.published(corrupt,lambda:False,Mock())


def test_publication_and_staging_reject_missing_git_provenance(tmp_path):
    source=release_source();source.git_source=None
    with pytest.raises(ValueError,match='Git source provenance required'):source.publish(tmp_path)
    with pytest.raises(ValueError,match='Git source provenance required'):source.stage({}, {})
    assert not (tmp_path/'desired').exists()


def test_pre_git_history_can_be_replaced_but_never_deployed_or_rolled_back(tmp_path):
    source=release_source()
    path=source.publish(tmp_path)
    value=json.loads(path.read_text());value.pop('git_source')
    # Model a valid historical pre-Git publication with its original content hash.
    content={key:value[key] for key in ('schema','revision','files','supervisor_source','supervisor_sha256','components')}
    identity=hashlib.sha256(json.dumps(content,sort_keys=True).encode()).hexdigest()
    value['artifact_versions']={identity:1}
    path.write_text(json.dumps(value))
    with pytest.raises(ValueError,match='Git source provenance required'):
        LauncherUpdates.published(path,lambda:False,Mock())
    updated=source.publish(tmp_path)
    assert updated!=path
    assert source.generation==2
    with pytest.raises(ValueError,match='Git source provenance required'):LauncherUpdates.rollback(tmp_path)
    assert (tmp_path/'desired').resolve()==updated
