import hashlib
import json
from pathlib import Path
from unittest.mock import Mock
import pytest
from agent_updates import LauncherUpdates
from scripts import launcher_update


def test_publication_is_immutable_and_uses_existing_agent_revision(tmp_path):
    source=LauncherUpdates(Path(__file__).parents[1],lambda:False,Mock())
    path=source.publish(tmp_path)
    loaded=LauncherUpdates.published(tmp_path/'desired',lambda:False,Mock())
    assert loaded.revision==source.revision
    assert loaded.files==source.files
    assert path==source.publish(tmp_path)
    assert 'durable-docker-workers-v1' in loaded.capabilities
    assert not any(word in path.read_text() for word in ('controller_private_key','controller_signing_key'))


def test_corrupt_publication_is_rejected(tmp_path):
    source=LauncherUpdates(Path(__file__).parents[1],lambda:False,Mock())
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
    source=LauncherUpdates(Path(__file__).parents[1],lambda:False,Mock())
    first=source.publish(tmp_path)
    source.supervisor_source+='\n# next supervisor\n'
    second=source.publish(tmp_path)
    assert second!=first
    assert (tmp_path/'previous').resolve()==first
    source.publish(tmp_path)
    assert (tmp_path/'previous').resolve()==first
    restored=LauncherUpdates.rollback(tmp_path)
    assert restored.supervisor_source!=source.supervisor_source
    assert (tmp_path/'desired').resolve()==first
    assert (tmp_path/'previous').resolve()==second


def test_rollback_rejects_corrupt_previous_without_changing_desired(tmp_path):
    source=LauncherUpdates(Path(__file__).parents[1],lambda:False,Mock())
    current=source.publish(tmp_path)
    bad=tmp_path/'bad.json';bad.write_text('{}')
    (tmp_path/'previous').symlink_to(bad.name)
    with pytest.raises((ValueError,KeyError)):LauncherUpdates.rollback(tmp_path)
    assert (tmp_path/'desired').resolve()==current


def test_pager_component_sources_are_pinned_and_tampering_is_rejected(tmp_path):
    source=LauncherUpdates(Path(__file__).parents[1],lambda:False,Mock())
    path=source.publish(tmp_path)
    loaded=LauncherUpdates.published(path,lambda:False,Mock())
    assert loaded.components['public-pager']==source.components['public-pager']
    value=json.loads(path.read_text())
    value['components']['public-pager']['files']['scripts/swarm_paged_cache.py']+='\n# tampered\n'
    corrupt=tmp_path/'corrupt.json';corrupt.write_text(json.dumps(value))
    with pytest.raises(ValueError,match='component checksum'):LauncherUpdates.published(corrupt,lambda:False,Mock())
