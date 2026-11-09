import json
import pytest
import shard_logs


def test_only_observed_enrolled_worker_is_read(tmp_path,monkeypatch):
    config=tmp_path/'peers.json';config.write_text(json.dumps({'name':'owner','peers':{'owner':'127.0.0.1','peer':'10.42.0.2'}}))
    monkeypatch.setattr(shard_logs,'CONFIG',config)
    calls=[]
    def read(host,script,timeout):
        calls.append((host,script,timeout));return {'available':True,'text':'hello'}
    snapshot=lambda:{'items':[{'id':'queue/shard','worker':'peer','execution':{'container':'genomics-abc'}}]}
    assert shard_logs.tail('queue/shard',snapshot,read)['text']=='hello'
    assert calls[0][0]=={'host':'peer','local':False}
    assert "name='genomics-abc'" in calls[0][1]
    with pytest.raises(ValueError):shard_logs.tail('other',snapshot,read)
    assert len(calls)==1


def test_unstarted_job_does_not_contact_a_node():
    def fail(*a,**k):raise AssertionError('Unexpected log connection')
    assert not shard_logs.tail('q',lambda:{'items':[{'id':'q'}]},fail)['available']


def test_invalid_recorded_container_is_rejected(tmp_path,monkeypatch):
    config=tmp_path/'peers.json';config.write_text(json.dumps({'name':'owner','peers':{'owner':'127.0.0.1'}}));monkeypatch.setattr(shard_logs,'CONFIG',config)
    with pytest.raises(ValueError):shard_logs.tail('q',lambda:{'items':[{'id':'q','worker':'owner','container':'x;touch /tmp/bad'}]})
