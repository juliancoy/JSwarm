import time
import pytest
from scripts.launcher_agent import ResourceLease,job_sink


@pytest.mark.parametrize('blocked,fitting',[('cpu','gpu'),('gpu','cpu')])
def test_old_waiter_in_one_sink_does_not_block_other_sink(tmp_path,blocked,fitting):
    limits={'cpus':4,'memory_mb':4096,'available_mb':4096}
    active=ResourceLease('active',2,2048,directory=tmp_path,limits=limits,sink=blocked)
    waiting=ResourceLease('waiting',3,1024,directory=tmp_path,limits=limits,sink=blocked)
    other=ResourceLease('other',1,512,directory=tmp_path,limits=limits,sink=fitting)
    try:
        assert active.try_acquire()
        waiting.row['created']=time.time()-60;waiting.write()
        assert not waiting.try_acquire()
        assert other.try_acquire()
        assert active.row['active'] and other.row['active']
    finally:
        for lease in (other,waiting,active):lease.close()


def test_separate_sinks_share_physical_ram_and_keep_same_sink_fairness(tmp_path):
    limits={'cpus':8,'memory_mb':4096,'available_mb':4096}
    active=ResourceLease('active',1,3072,directory=tmp_path,limits=limits,sink='cpu')
    old=ResourceLease('old',1,2048,directory=tmp_path,limits=limits,sink='cpu')
    small=ResourceLease('small',1,512,directory=tmp_path,limits=limits,sink='cpu')
    gpu=ResourceLease('gpu',1,2048,directory=tmp_path,limits=limits,sink='gpu')
    try:
        assert active.try_acquire()
        old.row['created']=time.time()-60;old.write()
        assert not small.try_acquire() # Existing CPU anti-starvation rule retained.
        assert not gpu.try_acquire() # No duplicate RAM pool for the GPU.
        small.close()
        active.close()
        assert old.try_acquire()
        assert gpu.try_acquire()
    finally:
        for lease in (gpu,small,old,active):lease.close()


def test_ready_gpu_cannot_be_starved_by_new_cpu_jobs(tmp_path):
    limits={'cpus':8,'memory_mb':4096,'available_mb':4096}
    busy=ResourceLease('busy',1,3072,directory=tmp_path,limits=limits)
    gpu=ResourceLease('gpu',1,2048,directory=tmp_path,limits=limits,sink='gpu')
    cpu=ResourceLease('cpu',1,512,directory=tmp_path,limits=limits)
    try:
        assert busy.try_acquire()
        gpu.row['created']=time.time()-60;gpu.write()
        assert not gpu.try_acquire()
        busy.close()
        assert not cpu.try_acquire()
        assert gpu.try_acquire()
        assert cpu.try_acquire()
    finally:
        for lease in (cpu,gpu,busy):lease.close()


def test_capacity_reports_two_sinks_and_preserves_legacy_gpu_classification(tmp_path,monkeypatch):
    limits={'cpus':4,'memory_mb':4096,'available_mb':4096}
    monkeypatch.setattr(ResourceLease,'capacity',lambda self:limits)
    cpu=ResourceLease('cpu',1,512,directory=tmp_path)
    gpu=ResourceLease('gpu-qc-service',1,512,directory=tmp_path)
    try:
        assert cpu.try_acquire() and gpu.try_acquire()
        value=ResourceLease.pool_capacity(tmp_path)
        assert value['job_sinks']['cpu']['active_jobs']==1
        assert value['job_sinks']['gpu']['active_jobs']==1
        assert value['available_cpus']==2 and value['reserved_mb']==1024
        assert job_sink({'group':'gpu-qc-native'})=='gpu'
        with pytest.raises(ValueError):job_sink({'sink':'unknown'})
    finally:gpu.close();cpu.close()


def test_fractional_gpu_feeder_fits_shared_cpu_headroom_without_rounding(tmp_path):
    limits={'cpus':1,'memory_mb':4096,'available_mb':4096}
    cpu=ResourceLease('cpu',0.75,512,directory=tmp_path,limits=limits,sink='cpu')
    gpu=ResourceLease('gpu-mapper-service',0.25,512,directory=tmp_path,limits=limits,sink='gpu')
    excess=ResourceLease('another-feeder',0.125,128,directory=tmp_path,limits=limits,sink='gpu')
    try:
        assert cpu.try_acquire() and gpu.try_acquire()
        assert not excess.try_acquire()
        assert gpu.row['cpus']==0.25
    finally:
        for lease in (excess,gpu,cpu):lease.close()


@pytest.mark.parametrize('cpu',[0,-0.25,float('nan'),float('inf'),True,'0.25'])
def test_invalid_fractional_cpu_requests_fail_closed(tmp_path,cpu):
    with pytest.raises(ValueError,match='Invalid resources'):
        ResourceLease('gpu-mapper-service',cpu,128,directory=tmp_path)


def test_explicit_cpu_burst_is_bounded_and_does_not_relax_memory(tmp_path):
    limits={'cpus':1,'memory_mb':2048,'available_mb':2048}
    cpu=ResourceLease('cpu',1,1024,directory=tmp_path,limits=limits)
    feeder=ResourceLease('mapper',0.25,512,directory=tmp_path,limits=limits,sink='gpu',cpu_burst=True)
    excess=ResourceLease('another',0.25,128,directory=tmp_path,limits=limits,sink='gpu',cpu_burst=True)
    try:
        assert cpu.try_acquire() and feeder.try_acquire()
        assert not excess.try_acquire()
        feeder.close()
        too_much_memory=ResourceLease('mapper',0.25,1536,directory=tmp_path,limits=limits,sink='gpu',cpu_burst=True)
        try:assert not too_much_memory.try_acquire()
        finally:too_much_memory.close()
    finally:
        for lease in (excess,feeder,cpu):lease.close()


@pytest.mark.parametrize('cpu,sink',[(0.5,'gpu'),(0.25,'cpu')])
def test_cpu_burst_cannot_relax_general_jobs(tmp_path,cpu,sink):
    with pytest.raises(ValueError,match='quarter-CPU GPU feeders'):
        ResourceLease('job',cpu,128,directory=tmp_path,sink=sink,cpu_burst=True)
