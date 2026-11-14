import copy
import gzip
import hashlib
import io
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from scripts import launcher_streams as s
from tls_test_server import tls_server
from scripts.swarm_tls_transfer import identity


def request(**overrides):
    return {'id':'piece-1','recipe':'gzip.sha256.v1','image':'sha256:'+'1'*64,
            'inputs':{'data':{'peer':'source','machine':'2'*64,'object':'3'*64,'sha256':'4'*64,'bytes':123}},**overrides}


@pytest.mark.parametrize('change',[{'argv':['sh']},{'recipe':'unknown'},{'image':'latest'},{'buffer_bytes':128*1024**2},{'resources':{'cpus':0,'memory_mb':256}}])
def test_invalid_contracts_rejected(change):
    with pytest.raises(ValueError):s.validate(request(**change))


def test_no_size_limit_but_gzip_boundaries_required():
    r=request();r['inputs']['data']['bytes']=20*1024**4
    assert s.validate(r)['inputs']['data']['bytes']==20*1024**4
    r['inputs']['data']['start']=1
    with pytest.raises(ValueError,match='member-aligned'):s.validate(r)
    r['recipe']='bytes.sha256.v1'
    assert s.validate(r)


def test_durable_idempotent_journal_and_pause(tmp_path):
    p=tmp_path/'work.sqlite';j=s.Journal(p);spec=s.validate(request())
    row=j.submit(spec,True);assert row['state']=='queued'
    j.update(spec['id'],state='completed',result={'proof':1})
    j=s.Journal(p)
    assert j.submit(spec,False)['result']=={'proof':1}
    with pytest.raises(ValueError):j.submit({**spec,'label':'changed'},True)
    with pytest.raises(PermissionError):j.submit({**spec,'id':'other'},False)
    assert len(j.list())==1


class Lease:
    def __init__(self,*args,**kwargs):self.closed=False;self.available=True
    def try_acquire(self):return self.available
    def close(self):self.closed=True


@pytest.fixture
def engine(tmp_path,monkeypatch):
    config=tmp_path/'peers.json';config.write_text('{"name":"node"}')
    agent=SimpleNamespace(root=tmp_path,config=config)
    policy={'accepting_jobs':True}
    e=s.Engine(agent,Lease,lambda _:policy,start=False)
    e.bulk=lambda:{'root':str(tmp_path/'tls'),'config_dir':str(tmp_path),'config':{'peers':{'source':{'machine':'2'*64}}}}
    e.host_path=lambda p:str(p)
    e.worker_user=lambda:'1000:1000'
    e.policy_value=policy;e.containers={};e.commands=[]
    def inspect(row):return e.containers.get(row['container'])
    def command(args,**kwargs):
        e.commands.append(args)
        if args[:2]==['docker','run']:e.containers[args[args.index('--name')+1]]={'Running':True}
        if args[:2]==['docker','rm']:e.containers.pop(args[2],None)
        if args[:2]==['docker','stop']:e.containers[args[-1]]={'Running':False,'ExitCode':143}
        return SimpleNamespace(stdout='')
    e.inspect=inspect;e.command=command
    yield e
    e.close()


def test_pause_after_admission_and_recovery(engine):
    e=engine;e.dispatch('stream.submit',request());e.policy_value['accepting_jobs']=False;e.tick()
    assert not e.commands and e.store.get('piece-1')['state']=='queued'
    with pytest.raises(PermissionError):e.dispatch('stream.submit',request(id='other'))
    assert e.dispatch('stream.submit',request())['state']=='queued'
    e.policy_value['accepting_jobs']=True;e.tick();row=e.store.get('piece-1');assert row['state']=='running'
    e.pending.clear();e.tick()  # New launcher memory; existing worker continues.
    assert len([c for c in e.commands if c[:2]==['docker','run']])==1
    e.dispatch('stream.cancel',{'id':'piece-1'});finish_stop(e)
    assert e.store.get('piece-1')['state']=='cancelled' and not e.containers


def test_node_ram_only_policy_rejects_durable_work_and_checks_queued_work(engine):
    e=engine
    e.dispatch('stream.submit',request())
    s.atomic_json(e.agent.config.parent/'worker-storage.json',{'mode':'ram-only'})
    # Idempotent reads of an accepted contract remain valid after a policy change.
    assert e.dispatch('stream.capabilities',{})['storage_modes']==['ram-only']
    assert e.dispatch('stream.submit',request())['state']=='queued'
    with pytest.raises(ValueError,match='RAM-only'):
        e.dispatch('stream.submit',request(id='durable-new'))
    with pytest.raises(ValueError,match='RAM-only'):
        e.launch(e.store.get('piece-1'))
    assert not any(c[:2]==['docker','run'] for c in e.commands)
    ram=request(id='ram-new',storage={'mode':'ram-only','scratch_mb':64})
    assert e.dispatch('stream.submit',ram)['state']=='queued'
    e.agent.validate_worker_storage=lambda paths:{'validated':True}
    e.tick()
    argv=next(c for c in e.commands if c[:2]==['docker','run'])
    assert any(a.startswith('/work:rw,') for a in argv)
    assert e.store.get('ram-new')['state']=='running'


def test_ram_only_policy_does_not_stop_running_worker(engine):
    e=engine;e.dispatch('stream.submit',request());e.tick()
    s.atomic_json(e.agent.config.parent/'worker-storage.json',{'mode':'ram-only'})
    e.tick()
    assert e.store.get('piece-1')['state']=='running'
    assert len([c for c in e.commands if c[:2]==['docker','run']])==1


def test_ram_only_policy_rechecks_created_worker(engine):
    e=engine;e.dispatch('stream.submit',request());e.tick()
    row=e.store.get('piece-1');e.containers[row['container']]={'Running':False,'Status':'created'}
    s.atomic_json(e.agent.config.parent/'worker-storage.json',{'mode':'ram-only'})
    with pytest.raises(ValueError,match='RAM-only'):e.reconcile(row)
    assert not any(c[:2]==['docker','start'] for c in e.commands)


def test_cancel_allows_docker_its_full_grace_period(engine):
    from unittest.mock import Mock
    engine.dispatch('stream.submit',request());engine.tick()
    engine.command=Mock(wraps=engine.command)
    assert engine.dispatch('stream.cancel',{'id':'piece-1'})['state']=='stopping'
    finish_stop(engine)
    stop=next(c for c in engine.command.call_args_list if c.args[0][:2]==['docker','stop'])
    assert stop.kwargs['timeout']>int(stop.args[0][stop.args[0].index('--time')+1])


def test_lost_docker_start_ack_reattaches(engine):
    import subprocess
    e=engine;command=e.command
    def lost(args):
        command(args)
        if args[:2]==['docker','run']:raise subprocess.TimeoutExpired(args,30)
    e.command=lost;e.dispatch('stream.submit',request());e.tick();e.tick()
    assert e.store.get('piece-1')['state']=='running'
    assert len([c for c in e.commands if c[:2]==['docker','run']])==1


@pytest.mark.parametrize('code,oom,wanted',[(78,False,'failed_integrity'),(75,False,'retry_wait'),(137,True,'failed'),(1,False,'failed')])
def test_failure_classification(engine,monkeypatch,code,oom,wanted):
    # Classification must not depend on whether SQLite commits take longer
    # than the retry backoff on a busy deployment host.
    now=s.time.time();monkeypatch.setattr(s.time,'time',lambda:now)
    e=engine;e.dispatch('stream.submit',request());e.tick();row=e.store.get('piece-1')
    e.containers[row['container']]={'Running':False,'ExitCode':code,'OOMKilled':oom};e.tick()
    assert e.store.get('piece-1')['state']==wanted
    if code==78:
        with pytest.raises(ValueError,match='new work contract'):e.dispatch('stream.retry',{'id':'piece-1'})


def test_bad_receipt_does_not_block_other_jobs(engine):
    e=engine
    for ident in ('piece-1','piece-2'):e.dispatch('stream.submit',request(id=ident))
    e.tick();row=e.store.get('piece-1');s.atomic_json(Path(row['folder'])/'result.json',{'schema':s.VERSION})
    e.containers[row['container']]={'Running':False,'ExitCode':0};e.tick()
    assert e.store.get('piece-1')['state']=='failed_integrity'
    assert e.store.get('piece-2')['state']=='running'


def test_pages_do_not_starve_later_jobs(engine):
    e=engine
    for n in range(105):e.dispatch('stream.submit',request(id=f'piece-{n:03}'))
    e.tick();assert len(e.containers)==105
    assert e.dispatch('stream.list',{'limit':100})['next']=='piece-099'


def test_mtls_streaming_gzip_and_raw_window(tls_server,tmp_path):
    root,server,peer=tls_server;raw=b'ACGT'*70000;encoded=gzip.compress(raw);obj='3'*64
    path=root/'launcher.gz';path.write_bytes(encoded);sha=hashlib.sha256(encoded).hexdigest()
    (root/'objects.json').write_text(json.dumps({obj:{'path':str(path),'sha256':sha,'identity':identity(path.stat())}}))
    (root/'config.json').write_text(json.dumps({'machine':'worker','port':server.server_port,'peers':{'source':{**peer,'machine':'2'*64}}}))
    req=s.validate(request(buffer_bytes=4096,parameters={'expected_bytes':len(raw),'expected_sha256':hashlib.sha256(raw).hexdigest()}))
    req['inputs']['data'].update(bytes=len(encoded),sha256=sha)
    job={'request':req,'worker_machine':'worker','contract_sha256':s.digest(req),'recipe_fingerprint':'a'*64}
    result=s.execute_builtin(job,root,tmp_path/'result.json')
    s.validate_result({'spec':{**req,'recipe_fingerprint':'a'*64},'fingerprint':s.digest(req)},result,'worker')
    assert result['outputs']['bytes']==len(raw) and result['outputs']['whole_object_verified']
    assert not list(tmp_path.glob('*.gz'))
    req['parameters']['expected_sha256']='0'*64
    with pytest.raises(ValueError,match='Content digest'):s.execute_builtin(job,root,tmp_path/'wrong.json')
    assert not (tmp_path/'wrong.json').exists()
    req['recipe']='bytes.sha256.v1';req['parameters']={};req['inputs']['data'].update(start=1,length=10)
    result=s.execute_builtin(job,root,tmp_path/'range.json')
    assert result['outputs']['sha256']==hashlib.sha256(encoded[1:11]).hexdigest()
    assert result['outputs']['whole_object_verified'] is False


def test_shared_cache_once_corruption_repair_and_budget_fallback(tmp_path):
    raw=b'ACGT'*100;row={'sha256':hashlib.sha256(raw).hexdigest(),'bytes':len(raw)};calls=[]
    def create():calls.append(1);return io.BytesIO(raw)
    for _ in range(2):
        r=s.shared_input(row,create,tmp_path,10000);assert r.read()==raw;r.close()
    assert len(calls)==1
    (tmp_path/row['sha256']).write_bytes(b'bad')
    r=s.shared_input(row,create,tmp_path,10000);assert r.read()==raw;r.close();assert len(calls)==2
    other={'sha256':'c'*64,'bytes':100000}
    r=s.shared_input(other,create,tmp_path,1);assert r.read()==raw;r.close()


def test_active_shared_cache_not_evicted(tmp_path):
    raw=b'abc';row={'sha256':hashlib.sha256(raw).hexdigest(),'bytes':3}
    active=s.shared_input(row,lambda:io.BytesIO(raw),tmp_path,3)
    other={'sha256':hashlib.sha256(b'xyz').hexdigest(),'bytes':3}
    fallback=s.shared_input(other,lambda:io.BytesIO(b'xyz'),tmp_path,3)
    assert isinstance(fallback,io.BytesIO) and active.read()==raw
    active.close();fallback.close()


def test_reload_source_is_content_addressed(tmp_path):
    from scripts.launcher_agent import load_stream_runtime
    m=load_stream_runtime(tmp_path);assert m.VERSION==s.VERSION
    assert load_stream_runtime(tmp_path) is m
    Path(m.__file__).write_text('changed')
    with pytest.raises(ValueError,match='runtime changed'):load_stream_runtime(tmp_path)


def test_queued_contract_uses_persistent_runtime_after_agent_work_directory_changes(engine,tmp_path):
    engine.dispatch('stream.submit',request())
    pinned=engine.root.parent/'.stream-runtime'/engine.revision/'launcher_streams.py'
    pinned.parent.mkdir(parents=True);pinned.write_bytes(engine.runtime.read_bytes())
    engine.revision='f'*64
    engine.agent.root=tmp_path/'new-agent-working-directory'
    engine.tick()
    assert engine.store.get('piece-1')['state']=='running'
    argv=next(c for c in engine.commands if c[:2]==['docker','run'])
    assert any(str(pinned) in arg for arg in argv)


def test_cached_input_obeys_stream_buffer(tmp_path):
    raw=b'A'*10000;row={'sha256':hashlib.sha256(raw).hexdigest(),'bytes':len(raw)}
    cached=s.shared_input(row,lambda:io.BytesIO(raw),tmp_path,len(raw));cached.buffer=128
    with s.InputWindow(cached,0,len(raw)) as stream:
        assert len(stream.read())==128
        assert len(stream.read(10000))==128


def test_late_worker_recovery_does_not_require_second_resource_budget(engine):
    e=engine;e.dispatch('stream.submit',request());e.tick();row=e.store.get('piece-1')
    e.pending.clear();e.store.update(row['id'],state='admitting')
    def no_budget(*a,**kw):raise AssertionError('Existing worker must reattach without second reservation')
    e.lease_factory=no_budget;e.tick()
    assert e.store.get(row['id'])['state']=='running'


@pytest.mark.parametrize('op',['stream.submit','stream.status','stream.list','stream.retry','stream.cancel'])
def test_stream_control_requires_controller_signature(op):
    from scripts.launcher_agent import verify_controller
    with pytest.raises(ValueError,match='signature rejected'):
        verify_controller({'op':op,'from':'compai.local'},{'controller_public_key':'0'*64})


def test_enrolled_source_certificate_cannot_be_replaced(tls_server):
    root,server,peer=tls_server
    with s.RangeReader({'object':'0'*64,'bytes':1,'sha256':'0'*64},{**peer,'certificate_sha256':'0'*64},root,server.server_port) as stream:
        with pytest.raises(ValueError,match='certificate differs'):stream.read(1)


def test_recovery_rejects_unrelated_container(engine,monkeypatch):
    e=engine;e.dispatch('stream.submit',request());e.tick();row=e.store.get('piece-1')
    value={'Config':{'Labels':{'org.lifetech.stream-work':row['id'],'org.lifetech.stream-contract':row['fingerprint'],'org.lifetech.stream-attempt':'1'}},'Image':'sha256:'+'9'*64,'State':{'Running':True}}
    monkeypatch.setattr(s.subprocess,'run',lambda *a,**kw:SimpleNamespace(returncode=0,stdout=json.dumps([value])))
    with pytest.raises(ValueError,match='container identity'):s.Engine.inspect(e,row)


def test_only_owner_registry_can_declare_gpu_socket_mount(tmp_path):
    config=tmp_path/'peers.json';config.write_text('{}')
    recipe={'image':'sha256:'+'1'*64,'version':'2'*64,'argv':['python3','/kernel.py'],'mounts':[{'source':'/home/owner/gpu-qc','target':'/gpu-qc','readonly':True}]}
    path=tmp_path/'stream-recipes.json';path.write_text(json.dumps({'qc.v1':recipe}))
    assert s.recipes(config)['qc.v1']['mounts']==recipe['mounts']
    with pytest.raises(ValueError):s.validate(request(mounts=recipe['mounts']))
    recipe['mounts'][0]['target']='/tls'
    path.write_text(json.dumps({'qc.v1':recipe}))
    with pytest.raises(ValueError,match='mounts'):s.recipes(config)


def test_owner_can_install_recipe_without_restarting_active_workers(engine):
    e=engine
    row={'image':'sha256:'+'1'*64,'argv':['python3','/fixed.py'],'version':'2'*64}
    (e.agent.config.parent/'stream-recipes.json').write_text(json.dumps({'new.qc.v1':row}))
    assert 'new.qc.v1' in e.dispatch('stream.capabilities',{})['recipes']


def test_ram_only_contract_requires_charged_scratch_and_streamed_inputs():
    r=request(storage={'mode':'ram-only','scratch_mb':64})
    assert s.validate(r)['storage']['mode']=='ram-only'
    with pytest.raises(ValueError,match='headroom'):s.validate(request(storage={'mode':'ram-only','scratch_mb':250}))
    r['inputs']['data']['mode']='shared'
    with pytest.raises(ValueError,match='disk cache'):s.validate(r)


def test_ram_worker_payload_has_no_host_work_or_cache_mount(engine):
    e=engine;e.dispatch('stream.submit',request(storage={'mode':'ram-only','scratch_mb':64}));e.tick()
    args=next(c for c in e.commands if c[:2]==['docker','run'])
    mounts=[args[n+1] for n,a in enumerate(args) if a=='--mount']
    assert not any(m.endswith('dst=/work') or m.endswith('dst=/cache') for m in mounts)
    assert any(m.endswith('dst=/control') for m in mounts)
    assert '/work:rw,nosuid,nodev,noexec,size=64m,mode=1777' in args
    assert '--ram-worker' in args
    assert args[args.index('--memory')+1]==args[args.index('--memory-swap')+1]


def test_control_export_rejects_large_and_symlink_receipts(tmp_path):
    source=tmp_path/'payload';source.write_text('x'*1025)
    with pytest.raises(ValueError):s.export_control_file(source,tmp_path/'out',1024)
    alias=tmp_path/'alias';alias.symlink_to(source)
    with pytest.raises(ValueError):s.export_control_file(alias,tmp_path/'out',2048)
    assert not (tmp_path/'out').exists()


def test_docker_missing_object_case_is_not_an_admission_failure(engine,monkeypatch):
    import subprocess
    monkeypatch.setattr(s.subprocess,'run',lambda *a,**k:SimpleNamespace(returncode=1,stdout='[]',stderr='error: no such object: worker'))
    assert s.Engine.inspect(engine,{'container':'worker'}) is None


def test_gpu_inventory_wait_preserves_retries_and_original_queue_deadline(engine,monkeypatch):
    e=engine;clock=[100.0];monkeypatch.setattr(s.time,'time',lambda:clock[0])
    e.dispatch('stream.submit',request(lifecycle={'queue_seconds':30,'max_retries':0}))
    original=e.launch
    def waiting(row):
        # Simulate the admission checkpoint before a device probe fails.
        folder=e.root/'work'/row['id']/'1';folder.mkdir(parents=True,exist_ok=True)
        e.store.update(row['id'],state='admitting',attempt=1,folder=str(folder),container='swarm-job-test')
        e.pending[row['id']]=Lease()
        pending.append(e.pending[row['id']])
        raise s.GPUAdmissionWait('GPU inventory is stale')
    pending=[];monkeypatch.setattr(e,'launch',waiting)
    for now in (100.,103.,106.,109.):
        clock[0]=now;e.tick();row=e.store.get('piece-1')
        assert row['state']=='admitting' and row['retries']==0 and row['attempt']==1
        assert not e.pending and pending[-1].closed
    assert not e.commands
    monkeypatch.setattr(e,'launch',original);clock[0]=112.;e.tick()
    assert e.store.get('piece-1')['state']=='running'
    assert len([c for c in e.commands if c[:2]==['docker','run']])==1


def test_gpu_inventory_wait_does_not_extend_queue_deadline(engine,monkeypatch):
    e=engine;clock=[100.];monkeypatch.setattr(s.time,'time',lambda:clock[0])
    e.dispatch('stream.submit',request(lifecycle={'queue_seconds':5,'max_retries':0}))
    def waiting(row):raise s.GPUAdmissionWait('GPU allocation is unknown')
    monkeypatch.setattr(e,'launch',waiting);e.tick();clock[0]=106.;e.tick()
    row=e.store.get('piece-1')
    assert row['state'] in ('stopping','failed') and row['error']=='Queue deadline exceeded'


def test_rootless_worker_uses_host_owner_mapping(engine):
    import contextlib
    e=engine
    e.agent.client=lambda:contextlib.nullcontext(SimpleNamespace(info=lambda:{'SecurityOptions':['name=rootless']}))
    assert s.Engine.worker_user(e)=='0:0'


def test_owner_attested_mount_mapping_survives_missing_launcher_metadata(engine,monkeypatch):
    import contextlib
    e=engine;exists=Path.exists
    monkeypatch.setattr(Path,'exists',lambda p:True if str(p)=='/.dockerenv' else exists(p))
    def missing(name):raise LookupError('Launcher absent after live restore')
    e.agent.client=lambda:contextlib.nullcontext(SimpleNamespace(containers=SimpleNamespace(get=missing)))
    original=e.bulk
    e.bulk=lambda:{**original(),'host_paths':[{'container':str(e.root),'host':'/owner/launcher-state'}]}
    target=e.root/'work/task/job.json'
    assert s.Engine.host_path(e,target)=='/owner/launcher-state/work/task/job.json'


def test_status_reads_do_not_take_docker_admission_lock(engine):
    e=engine;e.dispatch('stream.submit',request())
    class AdmissionBusy:
        def __enter__(self):raise AssertionError('Progress blocked by admission')
        def __exit__(self,*args):pass
    old=e.lock;e.lock=AdmissionBusy()
    try:
        assert e.dispatch('stream.status',{'id':'piece-1'})['state']=='queued'
        assert len(e.dispatch('stream.list',{} )['items'])==1
    finally:e.lock=old


def test_stream_journal_can_live_outside_ephemeral_workspace(tmp_path):
    import shutil
    workspace=tmp_path/'checkout';workspace.mkdir()
    config=tmp_path/'config/peers.json';config.parent.mkdir();config.write_text('{"name":"node"}')
    agent=SimpleNamespace(root=workspace,config=config)
    state=config.parent/'durable-streams';state.mkdir()
    e=s.Engine(agent,Lease,lambda _: {'accepting_jobs':True},start=False,state_root=state)
    try:
        e.store.submit(s.validate(request()),True)
        shutil.rmtree(workspace)
        assert e.store.get('piece-1')['state']=='queued'
    finally:e.close()


def test_execution_timings_exclude_admission_and_prior_attempts(engine,monkeypatch):
    e=engine;clock=[100.0];monkeypatch.setattr(s.time,'time',lambda:clock[0])
    e.dispatch('stream.submit',request())
    clock[0]=200;e.store.update('piece-1',state='running',attempt=1)
    clock[0]=220;e.store.update('piece-1',state='retry_wait')
    clock[0]=500;e.store.update('piece-1',state='admitting',attempt=2)
    clock[0]=550
    assert e.dispatch('stream.timings',{'ids':['piece-1']})['items']['piece-1']['started'] is None
    clock[0]=600;e.store.update('piece-1',state='running')
    clock[0]=615
    assert e.dispatch('stream.status',{'id':'piece-1'})['execution']['elapsed_seconds']==15
    clock[0]=630;e.store.update('piece-1',state='completed',result={'finished':620})
    clock[0]=900
    timing=e.dispatch('stream.timings',{'ids':['piece-1','missing']})['items']
    assert set(timing)=={'piece-1'}
    assert timing['piece-1']['elapsed_seconds']==20
    assert timing['piece-1']['attempt']==2
    with pytest.raises(ValueError):e.dispatch('stream.timings',{'ids':['x']*101})


def test_gpu_compute_completion_releases_guard_without_completing_delivery():
    from scripts.launcher_streams import release_completed_gpu
    from unittest.mock import Mock
    guard=Mock()
    assert release_completed_gpu(guard,{'phase':'aligning','compute_complete':False}) is guard
    guard.close.assert_not_called()
    assert release_completed_gpu(guard,{'phase':'exporting-results','compute_complete':True}) is None
    guard.close.assert_called_once()
    assert release_completed_gpu(None,{'compute_complete':True}) is None


def test_gpu_release_requires_node_contract_confirmation(tmp_path):
    from scripts.launcher_streams import Engine
    from types import SimpleNamespace
    folder=tmp_path/'attempt';folder.mkdir()
    row={'id':'job','folder':str(folder),'fingerprint':'a'*64}
    engine=Engine.__new__(Engine);engine.machine='machine';engine.rows=lambda **kw:iter([row])
    proof=folder/'gpu-released.json'
    proof.write_text(json.dumps({'contract_sha256':'wrong','worker_machine':'machine'}))
    assert engine.released_gpu_work()==set()
    proof.write_text(json.dumps({'contract_sha256':'a'*64,'worker_machine':'machine'}))
    assert engine.released_gpu_work()=={'job'}


def finish_stop(engine,ident='piece-1'):
    engine.tick()
    future=engine.stops.get(ident)
    if future:future.result(timeout=5)
    engine.tick()


@pytest.mark.parametrize('limits',[{'bogus':1},{'stall_seconds':-1},{'max_retries':True},{'execution_seconds':float('inf')}])
def test_lifecycle_rejects_invalid_budgets(limits):
    with pytest.raises(ValueError,match='lifecycle'):s.validate(request(lifecycle=limits))


def test_cancel_intent_survives_restart_and_unknown_daemon(engine):
    e=engine;e.dispatch('stream.submit',request());e.tick()
    assert e.dispatch('stream.cancel',{'id':'piece-1'})['state']=='stopping'
    restored=s.Engine(e.agent,Lease,e.policy,start=False)
    restored.inspect=e.inspect;restored.command=e.command
    inspect=restored.inspect
    restored.inspect=lambda row:(_ for _ in ()).throw(RuntimeError('daemon unavailable'))
    try:
        restored.tick()
        future=restored.stops['piece-1']
        with pytest.raises(RuntimeError):future.result(timeout=5)
        restored.tick()
        assert restored.store.get('piece-1')['state']=='stopping'
        assert e.containers
        restored.inspect=inspect;restored.store.update('piece-1',due=0)
        finish_stop(restored)
        assert restored.store.get('piece-1')['state']=='cancelled'
        assert not e.containers
    finally:restored.close()


def test_slow_stop_does_not_block_status_or_other_jobs(engine):
    import threading
    e=engine;e.dispatch('stream.submit',request());e.tick();command=e.command
    from concurrent.futures import ThreadPoolExecutor
    entered=threading.Event();release=threading.Event()
    def slow(args,**kwargs):
        if args[:2]==['docker','stop']:
            entered.set();release.wait()
        return command(args,**kwargs)
    e.command=slow
    def exercise():
        e.dispatch('stream.cancel',{'id':'piece-1'});e.tick();assert entered.wait(30)
        assert e.dispatch('stream.status',{'id':'piece-1'})['state']=='stopping'
        e.dispatch('stream.submit',request(id='piece-2'));e.tick()
        assert e.store.get('piece-2')['state']=='running'
        with pytest.raises(ValueError,match='confirm'):e.dispatch('stream.remove',{'id':'piece-1'})
    pool=ThreadPoolExecutor(max_workers=1)
    try:pool.submit(exercise).result(timeout=30)
    finally:
        release.set();pool.shutdown(wait=True);finish_stop(e)
    assert e.store.get('piece-1')['state']=='cancelled'


def test_lost_stop_ack_never_claims_cancelled_until_confirmed(engine):
    import subprocess
    e=engine;e.dispatch('stream.submit',request());e.tick();command=e.command
    def lost(args,**kwargs):
        result=command(args,**kwargs)
        if args[:2]==['docker','stop']:raise subprocess.TimeoutExpired(args,45)
        return result
    e.command=lost;e.dispatch('stream.cancel',{'id':'piece-1'});e.tick()
    with pytest.raises(subprocess.TimeoutExpired):e.stops['piece-1'].result(timeout=5)
    e.tick();assert e.store.get('piece-1')['state']=='stopping'
    e.store.update('piece-1',due=0);finish_stop(e)
    assert e.store.get('piece-1')['state']=='cancelled' and not e.containers


def test_retry_budget_is_bounded_and_explicit_retry_resets_it(engine,monkeypatch):
    # Retry deadlines are logical time, independent of journal fsync latency.
    now=s.time.time();monkeypatch.setattr(s.time,'time',lambda:now)
    e=engine;req=request(lifecycle={'max_retries':1})
    e.dispatch('stream.submit',req);assert e.dispatch('stream.submit',req)['attempt']==0
    e.tick()
    for expected in ('retry_wait','failed'):
        row=e.store.get('piece-1');e.containers[row['container']]={'Running':False,'ExitCode':75}
        e.tick();assert e.store.get('piece-1')['state']==expected
        if expected=='retry_wait':e.store.update('piece-1',due=0);e.tick()
    assert 'retry budget exhausted' in e.store.get('piece-1')['error']
    e.dispatch('stream.retry',{'id':'piece-1'});e.tick()
    assert e.store.get('piece-1')['state']=='running' and e.store.get('piece-1')['retries']==0


def test_queue_and_execution_deadlines_are_node_owned(engine,monkeypatch):
    e=engine;now=s.time.time();monkeypatch.setattr(s.time,'time',lambda:now)
    e.policy_value['accepting_jobs']=True
    e.dispatch('stream.submit',request(lifecycle={'queue_seconds':10}));e.policy_value['accepting_jobs']=False
    now+=11;e.tick();assert e.store.get('piece-1')['state']=='failed' and not e.containers
    e.policy_value['accepting_jobs']=True
    e.dispatch('stream.submit',request(id='piece-2',lifecycle={'execution_seconds':10}));e.tick()
    now+=11;e.tick();assert e.store.get('piece-2')['state']=='stopping'
    finish_stop(e,'piece-2');assert e.store.get('piece-2')['state']=='failed'


def test_heartbeat_and_eta_changes_do_not_reset_stall_deadline(engine,monkeypatch):
    e=engine;now=s.time.time();monkeypatch.setattr(s.time,'time',lambda:now)
    e.dispatch('stream.submit',request(lifecycle={'stall_seconds':10}));e.tick();row=e.store.get('piece-1')
    path=Path(row['folder'])/'progress.json'
    s.atomic_json(path,{'phase':'aligning','completed_units':10,'updated':now,'execution_eta_seconds':100});e.tick()
    now+=8;s.atomic_json(path,{'phase':'aligning','completed_units':11,'updated':now});e.tick()
    now+=8;s.atomic_json(path,{'phase':'aligning','completed_units':11,'updated':now,'execution_eta_seconds':999});e.tick()
    assert e.store.get('piece-1')['state']=='running'
    now+=3;e.tick();assert e.store.get('piece-1')['state']=='stopping'
    finish_stop(e);assert e.store.get('piece-1')['state']=='failed'


def test_verified_ram_results_survive_delivery_outage(engine,monkeypatch):
    e=engine;now=s.time.time();monkeypatch.setattr(s.time,'time',lambda:now)
    e.dispatch('stream.submit',request(lifecycle={'stall_seconds':10,'execution_seconds':20}));e.tick();row=e.store.get('piece-1')
    s.atomic_json(Path(row['folder'])/'progress.json',{'phase':'exporting-results','compute_complete':True});e.tick()
    now+=86400;e.tick()
    assert e.store.get('piece-1')['state']=='running' and row['container'] in e.containers


def test_removal_archives_receipt_and_blocks_duplicate_execution(engine):
    e=engine;req=request();e.dispatch('stream.submit',req);e.tick();row=e.store.get('piece-1')
    e.store.update('piece-1',state='completed',result={'verified':'receipt'})
    with pytest.raises(ValueError,match='cleanup'):e.dispatch('stream.remove',{'id':'piece-1'})
    e.containers[row['container']]={'Running':False,'ExitCode':0}
    removed=e.dispatch('stream.remove',{'id':'piece-1'})
    assert removed['archived'] and removed['result']=={'verified':'receipt'}
    assert removed['execution'] is not None
    assert not e.store.list() and not e.containers
    assert (e.root/'archives/piece-1.json').exists() and not Path(row['folder']).exists()
    assert e.dispatch('stream.submit',req)['archived']
    assert len([cmd for cmd in e.commands if cmd[:2]==['docker','run']])==1
    with pytest.raises(ValueError,match='immutable contract'):e.dispatch('stream.submit',request(label='changed'))
    with pytest.raises(ValueError,match='Archived'):e.dispatch('stream.retry',{'id':'piece-1'})
    assert e.dispatch('stream.remove',{'id':'piece-1'})['archived']


def test_retention_never_removes_active_or_unconfirmed_worker(engine,monkeypatch):
    e=engine;e.dispatch('stream.submit',request());e.tick();row=e.store.get('piece-1')
    now=s.time.time()+8*86400;monkeypatch.setattr(s.time,'time',lambda:now)
    e.store.update('piece-1',state='failed')
    with e.store.connect() as db:db.execute('UPDATE work SET updated=? WHERE id=?',(now-8*86400,'piece-1'))
    e.maintenance_at=0;e.tick()
    assert not e.store.get('piece-1').get('archived') and row['container'] in e.containers
    e.containers[row['container']]={'Running':False,'ExitCode':1};e.tick()
    now+=8*86400;e.maintenance_at=0;e.tick()
    assert e.store.get('piece-1')['archived']


def test_elapsed_retry_budget_includes_waiting_for_resources(engine,monkeypatch):
    e=engine;now=s.time.time();monkeypatch.setattr(s.time,'time',lambda:now)
    e.dispatch('stream.submit',request(lifecycle={'retry_seconds':10}));e.tick();row=e.store.get('piece-1')
    e.containers[row['container']]={'Running':False,'ExitCode':75};e.tick()
    e.store.update('piece-1',state='admitting',container=None)
    now+=11;e.tick()
    assert e.store.get('piece-1')['state']=='failed'


def test_explicit_retry_gets_new_queue_deadline(engine,monkeypatch):
    e=engine;now=s.time.time();monkeypatch.setattr(s.time,'time',lambda:now)
    e.dispatch('stream.submit',request(lifecycle={'queue_seconds':10}));e.store.update('piece-1',state='failed')
    now+=20;e.dispatch('stream.retry',{'id':'piece-1'});e.tick()
    assert e.store.get('piece-1')['state']=='running'


def test_archival_keeps_timing_queries_available(engine):
    e=engine;e.dispatch('stream.submit',request());e.tick();row=e.store.get('piece-1')
    e.containers[row['container']]={'Running':False,'ExitCode':1};e.tick()
    before=e.store.timings(['piece-1'])['piece-1'];e.dispatch('stream.remove',{'id':'piece-1'})
    after=e.dispatch('stream.timings',{'ids':['piece-1']})['items']['piece-1']
    assert before['elapsed_seconds']==after['elapsed_seconds'] and before['finished']==after['finished']


def test_terminal_accounting_is_committed_before_container_cleanup(engine):
    e=engine;e.dispatch('stream.submit',request());row=e.store.get('piece-1')
    folder=e.root/'fixture';folder.mkdir();e.store.update('piece-1',state='running',container='worker',folder=str(folder),attempt=1)
    row=e.store.get('piece-1');key=row['fingerprint']+':1'
    s.atomic_json(folder/'.execution-accounting.json',{'schema':'swarm-execution-accounting.v1','attempt_key':key,'started':'2026-01-01T00:00:00Z','finished':'2026-01-01T00:00:02Z','elapsed_seconds':2,'cpu_core_seconds':3,'cpu_accounting_coverage':'final-pre-exit','exit_code':4})
    state={'Running':False,'ExitCode':4,'StartedAt':'2026-01-01T00:00:00Z','FinishedAt':'2026-01-01T00:00:02Z'}
    original=e.command
    def command(args,**kw):
        if args[:2]==['docker','rm']:
            timing=e.store.timings(['piece-1'])['piece-1']
            assert timing['cpu_core_seconds']==3
            assert timing['elapsed_seconds']==2
        return original(args,**kw)
    e.command=command;e.reconcile(row,state)
    assert e.store.get('piece-1')['state']=='failed'
    assert s.Journal(e.store.path).timings(['piece-1'])['piece-1']['cpu_accounting_coverage']=='final-pre-exit'


def test_forced_kill_without_receipt_does_not_fabricate_cpu_time(engine):
    e=engine;e.dispatch('stream.submit',request());folder=e.root/'fixture';folder.mkdir();e.store.update('piece-1',state='running',container='worker',folder=str(folder),attempt=1)
    e.reconcile(e.store.get('piece-1'),{'Running':False,'ExitCode':137,'OOMKilled':True,'StartedAt':'2026-01-01T00:00:00Z','FinishedAt':'2026-01-01T00:00:03Z'})
    timing=e.store.timings(['piece-1'])['piece-1']
    assert timing['elapsed_seconds']==3
    assert timing['cpu_accounting_coverage']=='unavailable'
    assert 'cpu_core_seconds' not in timing


def test_queue_wait_is_separate_from_execution_and_prior_attempts(engine,monkeypatch):
    e=engine;clock=[100.0];monkeypatch.setattr(s.time,'time',lambda:clock[0]);e.dispatch('stream.submit',request())
    clock[0]=200;e.store.update('piece-1',state='running',attempt=1)
    clock[0]=220;e.store.update('piece-1',state='retry_wait')
    clock[0]=300;e.store.update('piece-1',state='admitting',attempt=2)
    clock[0]=400;e.store.update('piece-1',state='running')
    clock[0]=450;e.store.update('piece-1',state='failed')
    value=e.store.timings(['piece-1'])['piece-1']
    assert value['queue_seconds']==280
    assert value['elapsed_seconds']==50
    assert value['attempt']==2


def test_cached_file_cleanup_waits_for_all_queued_jobs(engine):
    e=engine
    first=request();first['inputs']['data']['mode']='shared'
    e.dispatch('stream.submit',first)
    second=request(id='second');second['inputs']['data']['mode']='shared'
    e.dispatch('stream.submit',second)
    folder=e.root/'cache';folder.mkdir()
    path=folder/first['inputs']['data']['sha256'];path.write_bytes(b'cached payload')
    path.with_suffix('.json').write_text('{}')
    e.store.update('piece-1',state='completed')
    e.release_job_cache(e.store.get('piece-1'))
    assert path.exists()  # The second job has not even opened the cache yet.
    e.store.update('second',state='cancelled')
    e.release_job_cache(e.store.get('second'))
    assert not path.exists() and not path.with_suffix('.json').exists()
