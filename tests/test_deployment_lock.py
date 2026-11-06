import json
import os
from pathlib import Path
import subprocess
import sys
from unittest.mock import Mock

import pytest
import agent_updates as updates


@pytest.fixture
def source():
    return updates.LauncherUpdates(Path(__file__).parents[1],lambda:False,Mock())


def test_process_lock_reports_owner_and_releases_after_crash(tmp_path):
    code="""import signal,sys
from pathlib import Path
from agent_updates import deployment_lock
with deployment_lock(folder=Path(sys.argv[1]),operation='test rollout'):
 print('locked',flush=True)
 signal.pause()
"""
    env={**os.environ,'PYTHONPATH':str(Path(updates.__file__).parent)}
    child=subprocess.Popen([sys.executable,'-u','-c',code,str(tmp_path)],stdout=subprocess.PIPE,text=True,env=env)
    try:
        assert child.stdout.readline().strip()=='locked'
        owner=json.loads((tmp_path/'input-deployment.lock').read_text())
        assert owner['pid']==child.pid and owner['operation']=='test rollout'
        with pytest.raises(RuntimeError,match='Deployment locked by test rollout'):
            with updates.deployment_lock(folder=tmp_path):pytest.fail('Competing writer acquired lock')
    finally:
        child.terminate();child.wait(timeout=10);child.stdout.close()
    with updates.deployment_lock(folder=tmp_path):pass


def test_publication_cannot_bypass_an_active_rollout(tmp_path,source):
    folder=tmp_path/'releases';folder.mkdir()
    aliases=tmp_path/'other';aliases.mkdir()
    alias=aliases/'release-alias';alias.symlink_to(folder,target_is_directory=True)
    code="""import signal,sys
from pathlib import Path
from agent_updates import deployment_lock
with deployment_lock(folder=Path(sys.argv[1]),operation='fleet rollout'):
 print('locked',flush=True)
 signal.pause()
"""
    child=subprocess.Popen([sys.executable,'-u','-c',code,str(tmp_path)],stdout=subprocess.PIPE,text=True,
                           env={**os.environ,'PYTHONPATH':str(Path(updates.__file__).parent)})
    try:
        assert child.stdout.readline().strip()=='locked'
        with pytest.raises(RuntimeError,match='fleet rollout'):source.publish(folder)
        with pytest.raises(RuntimeError,match='fleet rollout'):source.publish(alias)
        assert not (folder/'desired').exists()
    finally:
        child.terminate();child.wait(timeout=10);child.stdout.close()


def test_stale_validation_cannot_replace_an_advanced_publication(tmp_path,source):
    expected=updates.publication_token(tmp_path)
    first=source.publish(tmp_path,expected=expected)
    source.supervisor_source+='\n# newer validated source\n'
    second=source.publish(tmp_path,expected=first.name)
    with pytest.raises(RuntimeError,match='advanced during preparation'):
        source.publish(tmp_path,expected=first.name)
    assert (tmp_path/'desired').resolve()==second


def test_generations_advance_and_old_code_requires_explicit_rollback(tmp_path,source):
    first=source.publish(tmp_path)
    old=updates.LauncherUpdates.published(first,lambda:False,Mock())
    source.supervisor_source+='\n# next release\n'
    second=source.publish(tmp_path)
    assert source.generation==2
    with pytest.raises(RuntimeError,match='explicit rollback'):old.publish(tmp_path)
    assert (tmp_path/'desired').resolve()==second
    restored=updates.LauncherUpdates.rollback(tmp_path)
    assert restored.generation==3 and restored.supervisor_source==old.supervisor_source
    restored.supervisor_source+='\n# forward after rollback\n'
    third=restored.publish(tmp_path)
    assert restored.generation==4
    assert json.loads(third.read_text())['predecessor'].startswith('00000000000000000003-')


def test_publication_uses_the_existing_lock_reentrantly(tmp_path,source):
    with updates.deployment_lock(folder=tmp_path.parent,operation='validate and publish'):
        first=source.publish(tmp_path)
        assert source.publish(tmp_path)==first
        assert source.generation==1


def test_old_source_with_new_component_content_cannot_masquerade_as_a_new_version(tmp_path,source):
    import hashlib
    first=source.publish(tmp_path)
    old=updates.LauncherUpdates.published(first,lambda:False,Mock())
    source.files['launcher_agent.py']+='\n# newer node source\n'
    source.revision=hashlib.sha256(b''.join(source.files[name].encode() for name in updates.FILES)).hexdigest()
    second=source.publish(tmp_path)
    old.supervisor_source+='\n# different packaging cannot upgrade older node code\n'
    with pytest.raises(RuntimeError,match='Older source version'):old.publish(tmp_path)
    assert (tmp_path/'desired').resolve()==second
    restored=updates.LauncherUpdates.rollback(tmp_path)
    assert restored.generation==3
    # Restoring the newer known code is forward progress, even after rollback.
    forward=source.publish(tmp_path)
    assert json.loads(forward.read_text())['generation']==4


def test_interrupted_publication_does_not_prevent_retry_or_reuse_a_generation(tmp_path,source,monkeypatch):
    first=source.publish(tmp_path)
    source.supervisor_source+='\n# retryable publication\n'
    replace=Path.replace
    def interrupted(path,target):
        if path.name=='desired.tmp':raise OSError('simulated interrupted activation')
        return replace(path,target)
    with monkeypatch.context() as patch:
        patch.setattr(Path,'replace',interrupted)
        with pytest.raises(OSError,match='interrupted activation'):source.publish(tmp_path)
    assert (tmp_path/'desired').resolve()==first
    recovered=source.publish(tmp_path)
    assert json.loads(recovered.read_text())['generation']==3
