from task_machines import capacity,project,snapshot
import pytest


def test_capacity_missing_is_not_zero_and_values_are_validated():
    assert capacity(None,100) is None
    assert capacity(True,100) is None
    assert capacity(float('nan'),100) is None
    assert capacity(101,100) is None
    assert capacity(0,100)['available_percent']==0
    assert capacity(25,100)['available_percent']==25


def test_machine_resources_are_own_node_metrics_not_reservations():
    data=project('node',{'source_revision':'release','worker_policy':{'mode':'always'}},
        {'status':'live','ram_total_bytes':1000,'ram_used_bytes':250,
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


def test_unenrolled_host_cannot_trigger_diagnostics(monkeypatch,tmp_path):
    import task_machines
    config=tmp_path/'peers.json';config.write_text('{"peers":{"node":"10.0.0.2"}}')
    monkeypatch.setattr(task_machines,'CONFIG',config)
    with pytest.raises(ValueError):snapshot('http://untrusted/')
