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


def test_unenrolled_host_cannot_trigger_diagnostics(monkeypatch,tmp_path):
    import task_machines
    config=tmp_path/'peers.json';config.write_text('{"peers":{"node":"10.0.0.2"}}')
    monkeypatch.setattr(task_machines,'CONFIG',config)
    with pytest.raises(ValueError):snapshot('http://untrusted/')
