"""Compatibility checks for Docker-backed workers, without renewable ownership."""
import json
import subprocess
import sys
import threading
from unittest.mock import patch
import pytest
from scripts import launcher_agent as module
from scripts.launcher_agent import Agent,ResourceLease,launcher_lock,docker_workers


def agent(tmp_path):
    a=Agent.__new__(Agent);a.root=tmp_path;a.config=tmp_path/'config.json';a.lock=threading.RLock()
    a.busy=lambda:None;a.pending_resources={}
    return a


def request():
    return {'id':'a'*32,'group':'genomics-test','cpus':1,'memory_mb':128,'container':'genomics-'+'a'*32}


def test_running_worker_is_rediscovered_without_lease_records(tmp_path,monkeypatch):
    live=[];monkeypatch.setattr(module,'docker_workers',lambda:live)
    monkeypatch.setattr(module,'worker_policy',lambda *_:{'accepting_jobs':True})
    monkeypatch.setattr(ResourceLease,'capacity',lambda *_:{'cpus':4,'memory_mb':4096,'available_mb':4096})
    a=agent(tmp_path);args=request()
    assert a.dispatch('resource.acquire',args)['acquired']
    assert a.pending_resources
    live.append({**args,'id':'container-id','job':args['id'],'active':True,'created':0,'pids':32})
    assert a.dispatch('resource.renew',{'id':args['id']})['managed_by']=='docker'
    assert not a.pending_resources
    b=agent(tmp_path)
    assert b.dispatch('resource.renew',{'id':args['id']})['renewed']
    assert not list((tmp_path/'.resources').glob('*.json'))
    # Releasing a compatibility request cannot hide a running Docker worker.
    b.dispatch('resource.release',{'id':args['id']})
    assert b.reconcile_resources()==live
    live.clear()
    with pytest.raises(ValueError,match='not active'):b.dispatch('resource.renew',{'id':args['id']})


def test_drain_blocks_new_launches_without_interrupting_worker_checks(tmp_path,monkeypatch):
    args=request();live=[{**args,'id':'container-id','job':args['id']}]
    monkeypatch.setattr(module,'docker_workers',lambda:live)
    a=agent(tmp_path)
    with patch('scripts.launcher_agent.Path.exists',return_value=True):
        assert a.dispatch('resource.renew',{'id':args['id']})['renewed']
        with pytest.raises(RuntimeError,match='declining new work'):
            a.dispatch('resource.acquire',{**args,'id':'c'*32,'container':'genomics-'+'c'*32})


def test_drain_allows_already_accepted_resource_admission_to_finish(tmp_path,monkeypatch):
    from unittest.mock import Mock
    import time
    args=request();a=agent(tmp_path)
    monkeypatch.setattr(module,'docker_workers',lambda:[])
    monkeypatch.setattr(module,'worker_policy',lambda *_:{'accepting_jobs':False,'reason':'draining'})
    lease=Mock(row={k:v for k,v in args.items() if k!='id'})
    lease.try_acquire.return_value=True
    a.pending_resources[args['id']]={'lease':lease,'deadline':time.monotonic()+300}
    assert a.dispatch('resource.acquire',args)['acquired']
    lease.try_acquire.assert_called_once()


def test_singleton_lock_fences_other_process_and_releases_on_exit(tmp_path):
    path=tmp_path/'launcher.lock';lock=launcher_lock(path)
    code='from scripts.launcher_agent import launcher_lock; import sys; lock=launcher_lock(sys.argv[1])'
    try:
        r=subprocess.run([sys.executable,'-c',code,str(path)],capture_output=True,text=True)
        assert r.returncode!=0 and 'already running' in r.stderr
    finally:lock.close()
    assert subprocess.run([sys.executable,'-c',code,str(path)],capture_output=True).returncode==0


def test_docker_capacity_reads_limits_and_fails_closed_on_daemon_error(monkeypatch):
    calls=[]
    row={'Id':'f'*64,'Name':'/genomics-'+'a'*32,'State':{'Running':True},
         'Config':{'Labels':{'org.lifetech.job':'a'*32,'org.lifetech.group':'test'}},
         'HostConfig':{'NanoCpus':2_000_000_000,'Memory':0,'MemoryReservation':256*1024**2,'PidsLimit':32}}
    def run(cmd,**kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd,0,('f'*64+'\n') if cmd[1]=='ps' else json.dumps([row]),'')
    monkeypatch.setattr(module.subprocess,'run',run)
    assert docker_workers()[0]['cpus']==2
    assert docker_workers()[0]['memory_mb']==256
    monkeypatch.setattr(module.subprocess,'run',lambda cmd,**kw:subprocess.CompletedProcess(cmd,1,'','daemon unavailable'))
    with pytest.raises(RuntimeError,match='Docker unavailable'):docker_workers()


def test_pending_admission_counts_docker_without_double_counting(tmp_path,monkeypatch):
    (tmp_path/'docker-workers').touch();limits={'cpus':2,'memory_mb':512,'available_mb':512}
    live=[];monkeypatch.setattr(module,'docker_workers',lambda:live)
    a=ResourceLease('test',1,128,'genomics-'+'a'*32,directory=tmp_path,limits=limits)
    b=ResourceLease('other',2,256,'genomics-'+'b'*32,directory=tmp_path,limits=limits)
    try:
        assert a.try_acquire()
        live.append({**a.row,'id':'docker-id'})
        assert not b.try_acquire()
        assert sum(r['memory_mb'] for r in b.rows() if r['active'])==128
        a.close()
        assert not b.try_acquire() # Docker worker still consumes a CPU.
        live.clear();assert b.try_acquire()
    finally:a.close();b.close()


def test_generic_worker_names_are_bound_to_the_admitted_job(tmp_path,monkeypatch):
    monkeypatch.setattr(module,'docker_workers',lambda:[])
    monkeypatch.setattr(module,'worker_policy',lambda *_:{'accepting_jobs':True})
    monkeypatch.setattr(ResourceLease,'capacity',lambda *_:{'cpus':4,'memory_mb':4096,'available_mb':4096})
    monkeypatch.setenv('SWARM_RESOURCE_DIR',str(tmp_path/'resources'))
    a=agent(tmp_path);args={**request(),'container':'swarm-job-'+'a'*32,'group':'image-processing'}
    assert a.dispatch('resource.acquire',args)['acquired']
    a.dispatch('resource.release',{'id':args['id']})
    with pytest.raises(ValueError,match='job-bound'):
        a.dispatch('resource.acquire',{**args,'container':'swarm-job-'+'b'*32})


def test_docker_inspection_timeout_keeps_reservation_queued(tmp_path,monkeypatch):
    import subprocess
    import scripts.launcher_agent as module
    monkeypatch.setattr(module.ResourceLease,'capacity',lambda self:{'cpus':4,'memory_mb':4096,'available_mb':4096})
    (tmp_path/'docker-workers').touch()
    lease=module.ResourceLease('queued-test',1,128,'fixture-worker',directory=tmp_path)
    try:
        def unavailable():raise subprocess.TimeoutExpired(['docker','inspect','worker'],15)
        monkeypatch.setattr(module,'docker_workers',unavailable)
        assert lease.try_acquire() is False
        assert not lease.row['active'] and lease.path.exists()
        monkeypatch.setattr(module,'docker_workers',lambda:[])
        assert lease.try_acquire() is True
    finally:lease.close()


def test_rpc_authentication_does_not_take_job_lock_and_still_rejects_replay(tmp_path,monkeypatch):
    import asyncio,time,secrets
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from unittest.mock import Mock
    class JobLock:
        def __enter__(self):raise AssertionError('RPC authentication blocked on job lock')
        def __exit__(self,*args):pass
    a=agent(tmp_path);a.lock=JobLock();a.nonce_lock=threading.Lock();a.nonces={}
    key=secrets.token_bytes(32)
    a.config.write_text(json.dumps({'name':'worker','master':'worker','peers':{'controller':'10.0.0.1'},'key':key.hex()}))
    a.telemetry=Mock();a.telemetry.accept.return_value={'accepted':True};a.telemetry_policy=lambda:{}
    monkeypatch.setattr(module,'verify_controller',lambda *_:None)
    value={'to':'worker','from':'controller','time':time.time(),'nonce':'a'*32,'op':'metrics.publish','args':{}}
    cipher=AESGCM(key);nonce=secrets.token_bytes(12)
    raw=nonce+cipher.encrypt(nonce,json.dumps(value).encode(),module.VERSION.encode())
    reply=asyncio.run(a.process(raw));decoded=json.loads(cipher.decrypt(reply[:12],reply[12:],module.VERSION.encode()))
    assert decoded['ok'] and decoded['result']['accepted']
    with pytest.raises(ValueError,match='authentication rejected'):asyncio.run(a.process(raw))


def test_explicit_gpu_sink_is_idempotent_and_cannot_be_reassigned(tmp_path,monkeypatch):
    monkeypatch.setattr(module,'docker_workers',lambda:[])
    monkeypatch.setattr(module,'worker_policy',lambda *_:{'accepting_jobs':True})
    monkeypatch.setattr(ResourceLease,'capacity',lambda *_:{'cpus':4,'memory_mb':4096,'available_mb':4096})
    monkeypatch.setenv('SWARM_RESOURCE_DIR',str(tmp_path/'resources'))
    a=agent(tmp_path);args={**request(),'sink':'gpu'}
    try:
        assert a.dispatch('resource.acquire',args)['acquired']
        assert a.dispatch('resource.acquire',args)['acquired']
        assert a.pending_resources[args['id']]['lease'].row['sink']=='gpu'
        with pytest.raises(ValueError,match='sink already assigned'):
            a.dispatch('resource.acquire',{**args,'sink':'cpu'})
        with pytest.raises(ValueError):
            a.dispatch('resource.acquire',{**args,'sink':'invalid'})
    finally:a.dispatch('resource.release',{'id':args['id']})


def test_accepted_launch_reservation_survives_node_replacement(tmp_path,monkeypatch):
    monkeypatch.setattr(module,'docker_workers',lambda:[])
    monkeypatch.setattr(module,'worker_policy',lambda *_:{'accepting_jobs':True})
    monkeypatch.setattr(ResourceLease,'capacity',lambda *_:{'cpus':4,'memory_mb':4096,'available_mb':4096})
    a=agent(tmp_path);args=request()
    assert a.dispatch('resource.acquire',args)['acquired']
    lease=a.pending_resources[args['id']]['lease'];path=lease.path
    lease.file.close()  # The old process exits; accepted reservation stays durable.
    b=agent(tmp_path);b.recover_pending_resources()
    assert list(b.pending_resources)==[args['id']]
    assert b.pending_resources[args['id']]['lease'].path==path
    assert b.dispatch('resource.acquire',args)['acquired']
    b.dispatch('resource.release',{'id':args['id']})
    assert not path.exists()


def test_pressure_cpu_protects_unobserved_startups_and_ram(tmp_path):
    import time
    limits={'cpus':4,'memory_mb':4096,'available_mb':4096,'cpu_busy_fraction':0,'cpu_pressure_percent':1}
    first=ResourceLease('compute',4,1024,directory=tmp_path,limits=limits)
    extra=ResourceLease('another',2,1024,directory=tmp_path,limits=limits)
    excess=ResourceLease('excess',1,128,directory=tmp_path,limits=limits)
    try:
        assert first.try_acquire()
        first.row['cpu_observation']={'used':.1,'time':time.monotonic()};first.write();limits['cpu_busy_fraction']=.25
        assert extra.try_acquire()  # Six quota CPUs can share four physical CPUs.
        assert not excess.try_acquire()  # The newly admitted job is not measured yet.
        limits['available_mb']=100
        assert not excess.try_acquire()  # CPU elasticity never lends RAM.
    finally:
        for lease in (excess,extra,first):lease.close()


@pytest.mark.parametrize('busy,pressure,age,used',[(.9,1,0,.1),(.7,12,0,.1),(.2,1,11,.1),(.2,1,0,None),(.2,1,0,4)])
def test_cpu_lending_requires_fresh_idle_evidence(busy,pressure,age,used):
    import time
    limits={'cpus':4,'cpu_busy_fraction':busy,'cpu_pressure_percent':pressure}
    row={'cpus':4,'cpu_observation':{'used':used,'time':time.monotonic()-age}}
    assert module.cpu_headroom(limits,[row])['available_cpus']==0


def test_trailing_cpu_stalls_do_not_strand_idle_host_but_io_stalls_block():
    limits={'cpus':8,'cpu_busy_fraction':.2,'cpu_pressure_percent':25}
    assert module.cpu_headroom(limits,[])['available_cpus']==pytest.approx(6.4)
    assert module.cpu_headroom({**limits,'io_pressure_full_percent':20},[])['available_cpus']==0


def test_shared_cache_idle_charge_does_not_change_execution_quota():
    import time
    row={'cpus':1,'group':'shared-cache','cpu_observation':{'used':0,'time':time.monotonic()}}
    budget=module.cpu_headroom({'cpus':8,'cpu_busy_fraction':.2,'cpu_pressure_percent':0},[row])
    assert budget['charged_cpus']==pytest.approx(1.7) and budget['available_cpus']==pytest.approx(6.3)
    assert row['cpus']==1


def test_resource_priorities_reach_physical_pool(tmp_path):
    limits={'cpus':2,'memory_mb':4096,'available_mb':4096}
    low=ResourceLease('ordinary',2,128,directory=tmp_path,limits=limits)
    high=ResourceLease('qualification',2,128,directory=tmp_path,limits=limits,priority=100)
    try:
        assert not low.try_acquire()
        assert high.try_acquire()
        assert not low.try_acquire()
        high.close();assert low.try_acquire()
    finally:low.close();high.close()


def test_start_permit_is_short_and_renewable_without_expiring_running_jobs(tmp_path,monkeypatch):
    import time
    monkeypatch.setattr(module,'docker_workers',lambda:[])
    monkeypatch.setattr(module,'worker_policy',lambda *_:{'accepting_jobs':True})
    monkeypatch.setattr(ResourceLease,'capacity',lambda *_:{'cpus':4,'memory_mb':4096,'available_mb':4096})
    a=agent(tmp_path);args=request()
    try:
        assert a.dispatch('resource.acquire',args)['acquired']
        entry=a.pending_resources[args['id']]
        assert 0<entry['deadline']-time.monotonic()<=60
        entry['deadline']=time.monotonic()+1
        assert a.dispatch('resource.renew',{'id':args['id']})['renewed']
        assert entry['deadline']-time.monotonic()>50
        entry['deadline']=time.monotonic()-1
        a.reconcile_resources()
        assert not a.pending_resources
    finally:
        for entry in a.pending_resources.values():entry['lease'].close()



def test_actual_cpu_pressure_overrides_quota_totals():
    import time
    rows=[{'cpus':4,'cpu_observation':{'used':.2,'time':time.monotonic()}} for _ in range(5)]
    limits={'cpus':4,'cpu_busy_fraction':.25,'cpu_pressure_percent':0}
    result=module.cpu_headroom(limits,rows)
    assert result['reserved_cpus']==20
    assert result['available_cpus']==pytest.approx(2.5)
    assert result['cpu_quota_ceiling'] is None
    assert module.cpu_headroom({**limits,'cpu_busy_fraction':.95},[])['available_cpus']==0


def test_pressure_memory_lends_unused_limits_but_protects_peak_growth_and_startup():
    import time
    now=time.monotonic()
    limits={'memory_mb':16000,'available_mb':8000,'memory_pressure_some_percent':0,'memory_pressure_full_percent':0}
    row={'memory_mb':12000,'resident_mb':1000,'memory_observation':{'used':1000,'peak':1500,'growth':100,'first':now-60,'time':now}}
    budget=module.memory_budget(limits,[row])
    assert budget['memory_admission_mode']=='pressure'
    assert budget['memory_growth_mb']==950
    assert budget['available_mb']==6250  # 8000 real free - 800 system - 950 growth.
    startup={**row,'memory_observation':{**row['memory_observation'],'first':now}}
    assert module.memory_headroom(limits,[startup])==0
    stale={**row,'memory_observation':{**row['memory_observation'],'time':now-11}}
    assert module.memory_headroom(limits,[stale])==0
    assert module.memory_headroom(limits,[row,{'memory_mb':4000}])==2250


@pytest.mark.parametrize('field,value',[('memory_pressure_some_percent',1),('memory_pressure_full_percent',.1),('io_pressure_full_percent',20)])
def test_actual_stalls_block_memory_admission_even_with_no_reservations(field,value):
    limits={'memory_mb':16000,'available_mb':15000,'memory_pressure_some_percent':0,'memory_pressure_full_percent':0,field:value}
    assert module.memory_headroom(limits,[])==0


def test_pressure_memory_admission_is_atomic_for_competing_launches(tmp_path):
    import time
    now=time.monotonic()
    limits={'cpus':8,'memory_mb':16000,'available_mb':8000,'memory_pressure_some_percent':0,'memory_pressure_full_percent':0}
    live=ResourceLease('live',1,12000,directory=tmp_path,limits=limits)
    a=ResourceLease('new',1,4000,directory=tmp_path,limits=limits)
    b=ResourceLease('new',1,4000,directory=tmp_path,limits=limits)
    try:
        live.row.update(active=True,memory_observation={'used':1000,'first':now-60,'time':now});live.write()
        assert a.try_acquire()
        assert not b.try_acquire()  # First start's full RAM budget is protected.
    finally:
        for lease in (b,a,live):lease.close()



@pytest.mark.parametrize('busy,pressure',[(1,100),(.99,30),(None,None)])
def test_cpu_timesharing_admits_gpu_feeder_at_full_cpu_load(tmp_path,busy,pressure):
    limits={'cpus':4,'memory_mb':4096,'available_mb':4096,'cpu_admission_policy':'timeshare','cpu_busy_fraction':busy,'cpu_pressure_percent':pressure}
    cpu=ResourceLease('compute',4,512,directory=tmp_path,limits=limits)
    gpu=ResourceLease('gpu-feeder',2,512,directory=tmp_path,limits=limits,sink='gpu')
    try:
        assert cpu.try_acquire()
        assert gpu.try_acquire()
        result=module.cpu_headroom(limits,[cpu.row,gpu.row])
        assert result['available_cpus']==4 and result['reserved_cpus']==6
        assert result['cpu_admission_mode']=='timeshare'
    finally:gpu.close();cpu.close()


def test_cpu_timesharing_preserves_ram_and_pid_bounds(tmp_path):
    limits={'cpus':4,'memory_mb':1024,'available_mb':1024,'pids':64,'available_pids':64,'cpu_admission_policy':'timeshare','cpu_busy_fraction':1,'cpu_pressure_percent':100}
    cpu=ResourceLease('compute',4,768,directory=tmp_path,limits=limits,pids=32)
    gpu=ResourceLease('gpu-feeder',2,512,directory=tmp_path,limits=limits,pids=32,sink='gpu')
    try:
        assert cpu.try_acquire()
        assert not gpu.try_acquire()
        gpu.row['memory_mb']=128;gpu.row['pids']=64;gpu.write()
        assert not gpu.try_acquire()
        gpu.row['pids']=32;gpu.write()
        assert gpu.try_acquire()
    finally:gpu.close();cpu.close()
