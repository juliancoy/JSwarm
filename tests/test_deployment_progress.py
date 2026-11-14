import json
import time
from types import SimpleNamespace

import pytest
from scripts import deployment_progress as progress
from scripts.launcher_peers import NodePipelineWeb


def reader(config):
    node=NodePipelineWeb.__new__(NodePipelineWeb)
    node.agent=SimpleNamespace(config=config)
    return node


def test_queued_running_and_verified_nodes_are_reported(tmp_path):
    config=tmp_path/'peers.json'
    with progress.observe(config,'Fleet rollout'):
        assert reader(config).deployments()['deployments'][0]['status']=='queued'
        progress.update(status='running',revision='a'*64,nodes=[{'node':'worker.local','status':'queued'}])
        progress.node('worker.local','updating')
        assert reader(config).deployments()['deployments'][0]['nodes'][0]['status']=='updating'
        progress.node('worker.local','current',seconds=1.2)
    row=reader(config).deployments()['deployments'][0]
    assert row['status']=='complete' and row['finished_at']
    assert row['nodes']==[{'node':'worker.local','status':'current','seconds':1.2}]


def test_failure_is_retained_without_private_exception_text(tmp_path):
    config=tmp_path/'peers.json'
    with pytest.raises(RuntimeError):
        with progress.observe(config,'Validate and publish'):
            raise RuntimeError('private credential from subprocess')
    report=reader(config).deployments()
    assert report['deployments'][0]['status']=='failed'
    assert report['deployments'][0]['error']=='RuntimeError'
    assert 'private credential' not in json.dumps(report)


def test_stale_heartbeat_and_private_fields_are_not_live(tmp_path):
    config=tmp_path/'peers.json'
    item=progress.Progress(config,'Fleet rollout')
    value=dict(item.value,updated_at=time.time()-60,status='running',private_state='secret',nodes=[])
    item.path.write_text(json.dumps(value))
    report=reader(config).deployments()
    assert report['deployments'][0]['status']=='interrupted'
    assert 'secret' not in json.dumps(report)


def test_unsuccessful_rollout_is_not_marked_complete(tmp_path):
    config=tmp_path/'peers.json'
    with progress.observe(config,'Fleet rollout'):
        progress.update(status='failed',phase='Needs attention')
    assert reader(config).deployments()['deployments'][0]['status']=='failed'
