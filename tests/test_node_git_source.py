"""Real Git repositories exercise pinned source updates without Docker or VMs."""
import json
from pathlib import Path
import subprocess
import pytest
from scripts.node_git_source import git,snapshot,workspace


def repositories(tmp_path):
    root=tmp_path/'Organization'
    for folder in (root,root/'JSwarm',root/'GenomeHarbor'):
        folder.mkdir(parents=True,exist_ok=True)
        subprocess.run(['git','init','-q',str(folder)],check=True)
        git(folder,'config','user.name','Test')
        git(folder,'config','user.email','test@localhost')
        (folder/'source.py').write_text('version = 1\n')
        if folder==root:
            (folder/'.gitignore').write_text('/JSwarm/\n/GenomeHarbor/\n')
            git(folder,'add','.gitignore')
        git(folder,'add','source.py');git(folder,'commit','-qm','first')
        git(folder,'remote','add','origin',str(folder))
    return root


def test_git_update_is_pinned_and_preserves_previous_source_and_developer_edits(tmp_path):
    root=repositories(tmp_path);manifest=snapshot(root)
    first=workspace(manifest,tmp_path/'node-source')
    project=root/'JSwarm'
    (project/'source.py').write_text('version = 2\n')
    git(project,'commit','-qam','second')
    updated=snapshot(root)
    (project/'source.py').write_text('unsaved developer work\n')
    second=workspace(updated,tmp_path/'node-source')
    assert first!=second
    assert (first/'JSwarm/source.py').read_text()=='version = 1\n'
    assert (second/'JSwarm/source.py').read_text()=='version = 2\n'
    assert (project/'source.py').read_text()=='unsaved developer work\n'
    assert workspace(manifest,tmp_path/'node-source')==first
    result=subprocess.run(['git','-C',str(second/'JSwarm'),'symbolic-ref','-q','HEAD'],capture_output=True)
    assert result.returncode==1  # detached, no deployment branch


def test_changed_managed_checkout_is_rejected(tmp_path):
    root=repositories(tmp_path);manifest=snapshot(root)
    deployed=workspace(manifest,tmp_path/'node-source')
    (deployed/'JSwarm/source.py').write_text('tampered\n')
    with pytest.raises(RuntimeError,match='Managed Git source changed'):
        workspace(manifest,tmp_path/'node-source')


def test_workspace_preserves_cross_repository_relative_assets(tmp_path):
    root=repositories(tmp_path)
    view=root/'JSwarm/swarm-control/server/view'
    view.mkdir(parents=True)
    web=root/'GenomeHarbor/web';web.mkdir()
    (web/'genome.js').write_text('pinned genome asset\n')
    (view/'genome.js').symlink_to('../../../../GenomeHarbor/web/genome.js')
    git(root/'JSwarm','add','swarm-control')
    git(root/'JSwarm','commit','-qm','link genome asset')
    git(root/'GenomeHarbor','add','web')
    git(root/'GenomeHarbor','commit','-qm','genome asset')
    manifest=snapshot(root)
    deployed=workspace(manifest,tmp_path/'node-source')
    assert not (deployed/'JSwarm').is_symlink()
    assert (deployed/'JSwarm/swarm-control/server/view/genome.js').read_text()=='pinned genome asset\n'
    assert git(deployed,'rev-parse','HEAD')==manifest['Organization']['commit']
    assert workspace(manifest,tmp_path/'node-source')==deployed


def test_unavailable_commit_cannot_switch_existing_workspace(tmp_path):
    root=repositories(tmp_path);manifest=snapshot(root)
    deployed=workspace(manifest,tmp_path/'node-source')
    bad=json.loads(json.dumps(manifest));bad['JSwarm']['commit']='a'*40
    with pytest.raises(subprocess.CalledProcessError):workspace(bad,tmp_path/'node-source')
    assert (deployed/'JSwarm/source.py').read_text()=='version = 1\n'


def test_frozen_publication_records_git_commits(tmp_path):
    from scripts.deploy_swarm_inputs import freeze_source
    root=repositories(tmp_path)
    (root/'JSwarm/source.py').write_text('uncommitted\n')
    freeze_source(root,tmp_path/'snapshot')
    assert json.loads((tmp_path/'snapshot/JSwarm/.node-git-source.json').read_text())==snapshot(root)
    assert (tmp_path/'snapshot/JSwarm/source.py').read_text()=='version = 1\n'
