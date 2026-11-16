"""Host responsiveness reserve and sparse logical CPU affinity."""
import pytest
from scripts.launcher_agent import ResourceLease,cpu_headroom


def test_default_reserves_one_allowed_logical_cpu(monkeypatch):
    monkeypatch.delenv('SWARM_RESERVED_CPUS',raising=False)
    monkeypatch.setattr('os.sched_getaffinity',lambda _: {2,4,7,9})
    assert ResourceLease.cpu_allocation()=={'host_cpus':4,'reserved_logical_cpus':1,
                                         'cpus':3,'cpuset_cpus':'4,7,9'}


@pytest.mark.parametrize('reserved,cpus,mask',[('0',4,'2,4,7,9'),('2',2,'7,9'),('4',0,''),('99',0,'')])
def test_configurable_reserve_including_all_cpus(monkeypatch,reserved,cpus,mask):
    monkeypatch.setenv('SWARM_RESERVED_CPUS',reserved)
    monkeypatch.setattr('os.sched_getaffinity',lambda _: {2,4,7,9})
    allocation=ResourceLease.cpu_allocation()
    assert allocation['cpus']==cpus
    assert allocation['cpuset_cpus']==mask
    assert cpu_headroom({**allocation,'cpu_admission_policy':'timeshare'},[])['available_cpus']==cpus


@pytest.mark.parametrize('value',['-1','1.5','foo',''])
def test_invalid_reservation_rejected(monkeypatch,value):
    monkeypatch.setenv('SWARM_RESERVED_CPUS',value)
    with pytest.raises(ValueError,match='nonnegative integer'):ResourceLease.cpu_allocation()


def test_capacity_advertises_reduced_budget(monkeypatch):
    monkeypatch.setenv('SWARM_RESERVED_CPUS','1')
    monkeypatch.setattr('os.sched_getaffinity',lambda _: {0,1,2,3,4,5,6,7})
    capacity=ResourceLease.host_capacity()
    assert capacity['host_cpus']==8
    assert capacity['cpus']==7
    assert capacity['cpuset_cpus']=='1,2,3,4,5,6,7'
