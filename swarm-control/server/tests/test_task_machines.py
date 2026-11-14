from task_machines import capacity,project,snapshot
import pytest
import time


def test_capacity_missing_is_not_zero_and_values_are_validated():
    assert capacity(None,100) is None
    assert capacity(True,100) is None
    assert capacity(float('nan'),100) is None
    assert capacity(101,100) is None
    assert capacity(0,100)['available_percent']==0
    assert capacity(25,100)['available_percent']==25


def test_machine_resources_are_own_node_metrics_not_reservations():
    data=project('node',{'source_revision':'release','worker_policy':{'mode':'always'}},
        {'status':'live','sampled_at':time.time(),'ram_total_bytes':1000,'ram_used_bytes':250,
         'hardware':{'gpus':[{'name':'GPU','utilization_percent':20,'memory_total_mib':100,'memory_free_mib':40}]},
         'performance':{'filesystems':[{'device':'disk','mount':'/a','total_bytes':100,'available_bytes':60},
                                      {'device':'disk','mount':'/b','total_bytes':100,'available_bytes':60}]}},
         {'started_at':'start','uptime_seconds':120,'commit':'commit'})
    assert data['ram']['available_percent']==75
    assert len(data['disks'])==1 and data['disks'][0]['available_percent']==60
    assert data['gpus'][0]['available_percent']==80
    assert data['gpus'][0]['memory']['available_percent']==40
    assert data['commit']=='commit' and data['uptime_seconds']==120


def test_unknown_gpu_utilization_and_ram_stay_unknown():
    value=project('node',{}, {'hardware':{'gpus':[{'name':'detected GPU'}]}})
    assert value['ram'] is None and value['gpus'][0]['available_percent'] is None


def test_native_gpu_inventory_supplies_identity_and_its_own_fresh_measurement(monkeypatch):
    import task_machines
    monkeypatch.setattr(task_machines.time,'time',lambda:100)
    metrics={'status':'live','sampled_at':90,'hardware':{'gpus':[{'pci_id':'0000:04:00.0','name':'PCI device'}]}}
    inventory={'gpus':[{'pci_id':'00000000:04:00.0','uuid':'GPU-one','name':'RTX 3060','sampled_at':99,
                        'utilization_percent':0,'memory_total_mib':12288,'memory_free_mib':7763}]}
    value=project('node',{},metrics,gpu_inventory=inventory)
    assert value['gpus'][0]['name']=='RTX 3060' and value['gpus'][0]['available_percent']==100
    assert value['gpus'][0]['memory']['total_bytes']==12288*2**20
    inventory['gpus'][0]['sampled_at']=70
    value=project('node',{},metrics,gpu_inventory=inventory)
    assert value['gpus'][0]['status']=='stale' and value['gpus'][0]['available_percent'] is None and value['gpus'][0]['memory'] is None


def test_fresh_identity_does_not_renew_old_engine_measurements(monkeypatch):
    import task_machines
    monkeypatch.setattr(task_machines.time,'time',lambda:100)
    value=project('node',{}, {'status':'live','sampled_at':60,'hardware':{'gpus':[{'pci_id':'gpu','utilization_percent':80}]}},
        gpu_inventory={'gpus':[{'pci_id':'gpu','sampled_at':99}]})
    assert value['status']=='stale' and value['gpus'][0]['available_percent'] is None


def test_blocked_diagnostics_do_not_block_or_overwrite_fresh_gpu_inventory(monkeypatch):
    import threading
    from unittest.mock import Mock
    import task_machines
    entered=threading.Event();release=threading.Event();host={'host':'node'}
    task_machines.CACHE['node']={'value':{'status':'loading'},'busy':True}
    client=Mock();client.call.side_effect=lambda op,**kw: {'node':'node'} if op=='status' else {'status':'live','sampled_at':time.time()} if op=='metrics' else {'gpus':[{'name':'RTX','sampled_at':time.time(),'utilization_percent':25}]}
    monkeypatch.setattr(task_machines,'AgentClient',lambda _:client)
    def slow(_):entered.set();assert release.wait(3);return {'started_at':'2026-01-01T00:00:00+00:00'}
    monkeypatch.setattr(task_machines,'node_runtime',slow);monkeypatch.setattr(task_machines,'cpu_usage',lambda _:{});monkeypatch.setattr(task_machines,'probe',lambda *a:{'status':'local'})
    thread=threading.Thread(target=task_machines.diagnose,args=(host,'node'));thread.start()
    try:
        assert entered.wait(1)
        task_machines.collect(host,'node',{})
        value=task_machines.CACHE['node']['value'];assert value['gpus'][0]['available_percent']==75
        sampled=value['gpus'][0]['sampled_at'];assert not task_machines.CACHE['node']['busy']
    finally:release.set();thread.join(2)
    assert task_machines.CACHE['node']['value']['gpus'][0]['sampled_at']==sampled


def test_unenrolled_host_cannot_trigger_diagnostics(monkeypatch,tmp_path):
    import task_machines
    config=tmp_path/'peers.json';config.write_text('{"peers":{"node":"10.0.0.2"}}')
    monkeypatch.setattr(task_machines,'CONFIG',config)
    with pytest.raises(ValueError):snapshot('http://untrusted/')


def test_release_times_use_pinned_git_commit_not_current_checkout(tmp_path,monkeypatch):
    import json, subprocess
    from task_machines import release_times
    revision='a'*64;commit='b'*40;calls=[]
    def git(args,**kwargs):
        calls.append(args)
        return '2026-11-13T16:00:00-05:00\n'
    monkeypatch.setattr(subprocess,'check_output',git)
    (tmp_path/'0001-release.json').write_text(json.dumps({'schema':'jswarm-agent-release.v1','revision':revision,
        'git_source':{'repositories':{'JSwarm':{'commit':commit}}},'files':{'private':'not returned'}}))
    (tmp_path/'0002-invalid.json').write_text('{')
    rows=release_times(tmp_path,tmp_path)['releases']
    assert rows[revision]=={'released_at':'2026-11-13T16:00:00-05:00','release_commit':commit}
    assert calls==[['git','-C',str(tmp_path),'show','-s','--format=%cI',commit]]
    assert 'private' not in json.dumps(rows)


def test_release_time_recovers_after_git_object_arrives_without_publication_change(tmp_path,monkeypatch):
    import json,subprocess
    from task_machines import release_times
    revision='c'*64;commit='d'*40;calls=[]
    def git(args,**kwargs):
        calls.append(args)
        if len(calls)==1:raise subprocess.CalledProcessError(128,args)
        return '2026-11-13T19:55:32-05:00\n'
    monkeypatch.setattr(subprocess,'check_output',git)
    (tmp_path/'0001-release.json').write_text(json.dumps({'schema':'jswarm-agent-release.v1','revision':revision,
        'git_source':{'repositories':{'JSwarm':{'commit':commit}}}}))
    assert release_times(tmp_path,tmp_path)['releases']=={}
    assert release_times(tmp_path,tmp_path)['releases'][revision]['released_at']=='2026-11-13T19:55:32-05:00'
    assert release_times(tmp_path,tmp_path)['releases'][revision]['release_commit']==commit
    assert len(calls)==2
