import copy
import json
from pathlib import Path
from types import SimpleNamespace
import pytest
from scripts import release_validation as v


def release():
    return SimpleNamespace(files={'agent':'stable'},supervisor_source='stable',
        git_source={'installer':'stable','repositories':{name:{'origin':'https://example.test/'+name,'commit':'a'*40}
        for name in ('Organization','JSwarm','HarborGenetics')}},components={
        'private-filesystem':{'sha256':'private'},'public-pager':{'sha256':'public'}})


def test_private_only_runs_affected_checks_and_changed_tests():
    old=release();new=copy.deepcopy(old);new.components['private-filesystem']['sha256']='changed'
    suites=v.select(new,old,['full.py'],['scripts/swarm_fs_mount.py','tests/test_swarm_filesystem.py'])
    assert set(v.PRIVATE)<=set(suites) and 'full.py' not in suites
    assert 'tests/test_swarm_shared_cache.py' not in suites


@pytest.mark.parametrize('kind',['runtime','supervisor','installer','sibling','unknown_source','test_config'])
def test_uncertain_dependencies_require_full_gate(kind):
    old=release();new=copy.deepcopy(old);changed=[]
    if kind=='runtime':new.files['agent']='changed'
    elif kind=='supervisor':new.supervisor_source='changed'
    elif kind=='installer':new.git_source['installer']='changed'
    elif kind=='sibling':new.git_source['repositories']['Organization']['commit']='b'*40
    elif kind=='unknown_source':changed=['scripts/unmapped_dependency.py']
    else:changed=['tests/conftest.py']
    assert 'full.py' in v.select(new,old,['full.py'],changed)


def test_changed_workflow_and_tests_are_never_silently_reused():
    suites=v.select(release(),release(),['full.py'],['scripts/release_validation.py','tests/test_new_behavior.py'])
    assert set(v.WORKFLOW)<=set(suites)
    assert 'tests/test_new_behavior.py' in suites


def test_unknown_or_changed_component_checks():
    old=release();new=copy.deepcopy(old);new.components['public-pager']['sha256']='changed'
    assert set(v.PUBLIC)<=set(v.select(new,old,['full.py'],[]))
    new.components['unknown']={'sha256':'new'}
    assert 'full.py' in v.select(new,old,['full.py'],[])


def test_proof_is_bound_to_artifact_and_environment(tmp_path,monkeypatch):
    from agent_updates import LauncherUpdates
    artifact=tmp_path/'release.json';artifact.write_text('{}')
    (tmp_path/'desired').symlink_to(artifact.name)
    monkeypatch.setattr(v,'environment',lambda:'fixed')
    v.record(artifact,['full.py'])
    monkeypatch.setattr(LauncherUpdates,'published',lambda *a:release())
    monkeypatch.setattr(v,'changed_paths',lambda *a:[])
    suites,reused=v.choose(release(),tmp_path,tmp_path,['full.py'])
    assert reused==str(artifact) and 'full.py' not in suites
    artifact.write_text('{"tampered":true}')
    assert v.choose(release(),tmp_path,tmp_path,['full.py'])[1] is None
    artifact.write_text('{}');monkeypatch.setattr(v,'environment',lambda:'changed')
    assert v.choose(release(),tmp_path,tmp_path,['full.py'])[1] is None


def test_missing_success_evidence_cannot_skip_checks(tmp_path):
    artifact=tmp_path/'release.json';artifact.write_text('{}');(tmp_path/'desired').symlink_to(artifact.name)
    assert v.choose(release(),tmp_path,tmp_path,['full.py'])[1] is None


def test_environment_tracks_docker_configuration(tmp_path,monkeypatch):
    monkeypatch.setattr(Path,'home',lambda:tmp_path)
    folder=tmp_path/'.docker';folder.mkdir();config=folder/'config.json'
    config.write_text('{"currentContext":"default"}')
    first=v.environment();config.write_text('{"currentContext":"another"}')
    assert v.environment()!=first
