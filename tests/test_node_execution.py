import hashlib
import json
from pathlib import Path
import ssl
import threading
import time
from types import SimpleNamespace
import urllib.error
import urllib.request

import pytest
from scripts import launcher_streams as s
from scripts.swarm_tls_transfer import make_server
from test_launcher_streams import engine,request
from test_wgs_distributed_verify import tls_server


def test_direct_export_is_pinned_idempotent_and_never_overwrites(tls_server,tmp_path):
    root,_,_=tls_server
    objects=tmp_path/'objects';objects.mkdir();(tmp_path/'inboxes').mkdir();target=tmp_path/'outputs';target.mkdir()
    grant='a'*64;job_key='b'*64
    client=hashlib.sha256(ssl.PEM_cert_to_DER_cert((root/'identity.crt').read_text())).hexdigest()
    (tmp_path/'inboxes'/(grant+'.json')).write_text(json.dumps({'destination':str(target),'clients':[client],'job_key':job_key,'files':{'result.bam':100000},'max_bytes':100000,'expires':time.time()+60}))
    server=make_server('127.0.0.1',0,objects,root/'server.crt',root/'server.key',root/'ca.crt')
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    tls=tmp_path/'tls';tls.mkdir()
    for name in ('ca.crt','identity.crt','identity.key'):(tls/name).write_bytes((root/name).read_bytes())
    peer={'address':'127.0.0.1','machine':'c'*64,'certificate_sha256':hashlib.sha256(ssl.PEM_cert_to_DER_cert((root/'server.crt').read_text())).hexdigest()}
    (tls/'config.json').write_text(json.dumps({'machine':'d'*64,'port':server.server_port,'peers':{'source':peer}}))
    job={'worker_machine':'d'*64,'request':{'output':{'peer':'source','machine':'c'*64,'grant':grant,'job_key':job_key}}}
    result=tmp_path/'result';result.write_bytes(b'bounded verified output'*1000)
    try:
        first=s.publish_outputs(job,{'result.bam':result},tls)
        assert s.publish_outputs(job,{'result.bam':result},tls)==first
        assert (target/'result.bam').read_bytes()==result.read_bytes()
        result.write_bytes(b'different output')
        with pytest.raises(ValueError,match='rejected'):s.publish_outputs(job,{'result.bam':result},tls)
        assert (target/'result.bam').read_bytes()!=result.read_bytes()
        with pytest.raises(ValueError):s.publish_outputs(job,{'../escape':result},tls)
        assert not list(target.glob('*.partial'))
    finally:server.shutdown();server.server_close();thread.join()


def test_output_grants_reject_unknown_peer_and_wrong_job_key(engine):
    r=request(output={'peer':'stranger','machine':'e'*64,'grant':'a'*64,'job_key':'b'*64})
    with pytest.raises(ValueError,match='enrolled'):engine.dispatch('stream.submit',r)


def test_gpu_uses_gpu_sink_and_survives_engine_reattachment(engine,tmp_path):
    gpu={'uuid':'GPU-'+'1'*8+'-'+'1'*4+'-'+'1'*4+'-'+'1'*4+'-'+'1'*12,'minimum_free_mib':512,'lock_directory':str(tmp_path/'locks')}
    definition={'image':'sha256:'+'1'*64,'version':'a'*64,'argv':['python3','/app/worker.py'],'gpu':gpu}
    (engine.agent.config.parent/'stream-recipes.json').write_text(json.dumps({'test.gpu.v1':definition}))
    leases=[];old=engine.lease_factory
    engine.lease_factory=lambda *a,**kw:(leases.append(kw) or old(*a,**kw))
    command=engine.command
    def call(args):
        if args[0]=='nvidia-smi':return SimpleNamespace(stdout=gpu['uuid']+', 12000\n')
        if args[:2]==['docker','ps']:return SimpleNamespace(stdout='')
        return command(args)
    engine.command=call
    engine.dispatch('stream.submit',request(recipe='test.gpu.v1'));engine.tick()
    row=engine.store.get('piece-1');assert row['state']=='running'
    argv=next(c for c in engine.commands if c[:2]==['docker','run'])
    assert argv[argv.index('--gpus')+1]=='device='+gpu['uuid']
    assert '--recipe-worker' in argv and leases[0]['sink']=='gpu'
    job=json.loads((Path(row['folder'])/'job.json').read_text());assert job['gpu_lock'].endswith(gpu['uuid']+'.lock')
    engine.pending.clear();engine.tick()
    assert len([c for c in engine.commands if c[:2]==['docker','run']])==1


def test_unavailable_gpu_never_launches_cpu_fallback(engine,tmp_path):
    gpu={'uuid':'GPU-11111111-1111-1111-1111-111111111111','minimum_free_mib':512,'lock_directory':str(tmp_path/'locks')}
    (engine.agent.config.parent/'stream-recipes.json').write_text(json.dumps({'test.gpu.v1':{'image':'sha256:'+'1'*64,'version':'a'*64,'argv':['python3','/app/worker.py'],'gpu':gpu}}))
    command=engine.command
    engine.command=lambda args:SimpleNamespace(stdout='existing-gpu-worker') if args[:2]==['docker','ps'] else command(args)
    engine.dispatch('stream.submit',request(recipe='test.gpu.v1'));engine.tick()
    assert engine.store.get('piece-1')['state']=='retry_wait'
    assert not any(c[:2]==['docker','run'] for c in engine.commands)


def test_shared_reference_contract_is_content_bound():
    from scripts.swarm_shared_cache import contract
    files={'index':{'bytes':5,'sha256':'a'*64}};key,value=contract('sha256:'+'1'*64,files)
    cache={**value,'key':key,'container':'swarm-job-'+key[:32],'volume':'swarm-cache-'+key[:32],'memory_mb':145,'shm_mb':17}
    s.validate_execution({'shared_cache':cache})
    with pytest.raises(ValueError):s.validate_execution({'shared_cache':{**cache,'volume':'unrelated'}})
    with pytest.raises(ValueError):s.validate_execution({'shared_cache':{**cache,'files':{'index':{'bytes':6,'sha256':'a'*64}}}})


def test_accepted_recipe_is_pinned_across_registry_updates(engine):
    original={'image':'sha256:'+'1'*64,'version':'a'*64,'argv':['python3','/app/original.py']}
    path=engine.agent.config.parent/'stream-recipes.json';path.write_text(json.dumps({'test.app.v1':original}))
    engine.dispatch('stream.submit',request(recipe='test.app.v1'))
    replacement={**original,'image':'sha256:'+'2'*64,'argv':['python3','/app/replacement.py'],'version':'b'*64}
    path.write_text(json.dumps({'test.app.v1':replacement}))
    engine.dispatch('stream.capabilities',{})
    # Neither controller registry updates nor node reloads may rewrite work
    # already durably accepted under a different immutable recipe fingerprint.
    row=engine.store.get('piece-1');assert engine.definition(row['spec'])==original
    engine.tick()
    argv=next(c for c in engine.commands if c[:2]==['docker','run'])
    assert '/app/original.py' in argv and '/app/replacement.py' not in argv
    assert engine.dispatch('stream.submit',request(recipe='test.app.v1'))['state']=='running'


def test_priority_order_reprioritization_and_restart(engine):
    engine.dispatch('stream.submit',request(id='ordinary'))
    engine.dispatch('stream.submit',request(id='qualification',priority=100))
    engine.dispatch('stream.submit',request(id='urgent'))
    original=engine.store.get('urgent')['fingerprint']
    engine.dispatch('stream.priority',{'id':'urgent','priority':200})
    assert s.Journal(engine.store.path).get('urgent')['priority']==200
    assert engine.store.get('urgent')['fingerprint']==original
    assert engine.dispatch('stream.submit',request(id='urgent'))['priority']==200
    launched=[]
    engine.launch=lambda row:launched.append(row['id'])
    engine.tick()
    assert launched==['urgent','qualification','ordinary']
    for invalid in (True,1.5,1001):
        with pytest.raises(ValueError):engine.dispatch('stream.priority',{'id':'urgent','priority':invalid})


def test_priority_fifo_and_unavailable_job_does_not_block(engine):
    engine.dispatch('stream.submit',request(id='z-first',priority=100))
    engine.dispatch('stream.submit',request(id='a-second',priority=100))
    engine.dispatch('stream.submit',request(id='ordinary'))
    launched=[]
    def launch(row):
        launched.append(row['id'])
        if row['id']=='z-first':raise RuntimeError('GPU busy')
    engine.launch=launch
    engine.tick()
    assert launched==['z-first','a-second','ordinary']


def test_gpu_slots_allow_concurrency_and_release_after_exit(tmp_path,monkeypatch):
    ns={'__name__':'gpu_test'};exec(s.GPU_ADMISSION_SOURCE,ns)
    gpu={'uuid':'GPU-11111111-1111-1111-1111-111111111111','minimum_free_mib':512,'lock_directory':str(tmp_path),'max_concurrency':2,'vram_mb':512}
    monkeypatch.setattr(s.subprocess,'check_output',lambda *a,**k:gpu['uuid']+', 4096\n')
    first=ns['acquire_gpu'](gpu);second=ns['acquire_gpu'](gpu)
    try:
        with pytest.raises(BlockingIOError):ns['acquire_gpu'](gpu)
        second.close()
        with ns['acquire_gpu'](gpu):pass
        with pytest.raises(BlockingIOError):ns['acquire_gpu']({**gpu,'max_concurrency':1})
    finally:first.close();second.close()
    with ns['acquire_gpu']({**gpu,'max_concurrency':1}):
        with pytest.raises(BlockingIOError):ns['acquire_gpu'](gpu)


def test_gpu_slots_reserve_vram_atomically_and_ignore_dead_slot_records(tmp_path,monkeypatch):
    ns={'__name__':'gpu_test'};exec(s.GPU_ADMISSION_SOURCE,ns)
    gpu={'uuid':'GPU-11111111-1111-1111-1111-111111111111','minimum_free_mib':512,'lock_directory':str(tmp_path),'max_concurrency':4,'vram_mb':512}
    free=[2000]
    monkeypatch.setattr(s.subprocess,'check_output',lambda *a,**k:gpu['uuid']+', '+str(free[0])+'\n')
    first=ns['acquire_gpu'](gpu)
    try:
        free[0]=1400
        with pytest.raises(BlockingIOError,match='VRAM'):ns['acquire_gpu'](gpu)
    finally:first.close()
    with ns['acquire_gpu'](gpu):pass  # Stale unlocked JSON consumes no VRAM.


def test_installed_gpu_sharing_recipe_admits_multiple_workers_on_same_device(engine,tmp_path):
    gpu={'uuid':'GPU-11111111-1111-1111-1111-111111111111','minimum_free_mib':512,'lock_directory':str(tmp_path/'locks'),'max_concurrency':2,'vram_mb':512}
    (engine.agent.config.parent/'stream-recipes.json').write_text(json.dumps({'test.gpu.v1':{'image':'sha256:'+'1'*64,'version':'a'*64,'argv':['python3','/app/worker.py'],'gpu':gpu}}))
    command=engine.command
    def call(args):
        if args[0]=='nvidia-smi':return SimpleNamespace(stdout=gpu['uuid']+', 12000\n')
        if args[:2]==['docker','ps']:return SimpleNamespace(stdout='another-admitted-gpu-job')
        return command(args)
    engine.command=call
    for ident in ('first','second'):engine.dispatch('stream.submit',request(id=ident,recipe='test.gpu.v1'))
    engine.tick()
    for ident in ('first','second'):
        row=engine.store.get(ident);assert row['state']=='running'
        job=json.loads((Path(row['folder'])/'job.json').read_text())
        assert job['gpu_admission']=={**gpu,'lock_directory':'/gpu-locks'}


def amd_binding(tmp_path):
    return {'uuid':'DRM-'+'a'*32,'minimum_free_mib':512,'max_concurrency':1,'vram_mb':256,
            'render_device':'/dev/dri/renderD128','render_gid':992,'pci_id':'0000:04:00.0',
            'icd':'/usr/share/vulkan/icd.d/radeon_icd.json','runtime_directory':str(tmp_path),
            'runtime_files':{'bwa':'a'*64,'exact-extension.spv':'b'*64}}


def test_amd_device_binding_uses_render_node_and_pinned_runtime(tmp_path):
    from scripts.swarm_runtime import validate_gpu
    gpu=amd_binding(tmp_path);validate_gpu(gpu)
    args=s.gpu_container_args(gpu)
    assert '--gpus' not in args and args[args.index('--device')+1]=='/dev/dri/renderD128'
    assert 'VK_ICD_FILENAMES='+gpu['icd'] in args
    assert 'type=bind,src='+str(tmp_path)+',dst=/gpu-runtime,readonly' in args
    for change in ({'render_device':'/dev/mem'},{'runtime_files':{'bwa':'a'*64}},{'pci_id':'other'},{'uuid':'GPU-'+'1'*36}):
        with pytest.raises(ValueError):validate_gpu({**gpu,**change})



def test_amd_guard_uses_reserved_vram_and_rejects_artifact_changes(tmp_path,monkeypatch):
    ns={'__name__':'gpu_test'};exec(s.GPU_ADMISSION_SOURCE,ns)
    sysfs=tmp_path/'sysfs';device=tmp_path/'devices/0000:04:00.0';device.mkdir(parents=True)
    render=sysfs/'renderD128';render.mkdir(parents=True);(render/'device').symlink_to(device)
    (device/'vendor').write_text('0x1002')
    (device/'mem_info_vram_total').write_text(str(8192*1024**2));(device/'mem_info_vram_used').write_text(str(80*1024**2))
    runtime=tmp_path/'runtime';runtime.mkdir()
    files={}
    for name in ('bwa','exact-extension.spv'):
        (runtime/name).write_bytes(name.encode());files[name]=hashlib.sha256(name.encode()).hexdigest()
    locks=tmp_path/'locks';locks.mkdir()
    gpu={**amd_binding(runtime),'runtime_files':files,'lock_directory':str(locks)}
    ns['pathlib']=SimpleNamespace(Path=lambda p:sysfs if p=='/sys/class/drm' else runtime if p=='/gpu-runtime' else Path(p))
    monkeypatch.setattr(s.subprocess,'check_output',lambda *a,**k:(_ for _ in ()).throw(AssertionError('AMD must not call NVIDIA tools')))
    with ns['acquire_gpu'](gpu):pass
    (device/'mem_info_vram_used').write_text(str(8000*1024**2))
    with pytest.raises(BlockingIOError,match='AMD VRAM'):ns['acquire_gpu'](gpu)
    (runtime/'bwa').write_bytes(b'changed')
    with pytest.raises(ValueError,match='integrity'):ns['acquire_gpu'](gpu)



def test_existing_gpu_worker_reattaches_before_inventory_admission(engine,tmp_path):
    gpu={'uuid':'GPU-11111111-1111-1111-1111-111111111111','minimum_free_mib':512,'lock_directory':str(tmp_path/'locks')}
    (engine.agent.config.parent/'stream-recipes.json').write_text(json.dumps({'test.gpu.v1':{'image':'sha256:'+'1'*64,'version':'a'*64,'argv':['python3','/app/worker.py'],'gpu':gpu}}))
    original=engine.command
    engine.command=lambda args:SimpleNamespace(stdout=gpu['uuid']+', 12000\n') if args[0]=='nvidia-smi' else original(args)
    engine.dispatch('stream.submit',request(recipe='test.gpu.v1'));engine.tick()
    engine.store.update('piece-1',state='admitting')
    engine.agent.gpu_resources=lambda:(_ for _ in ()).throw(AssertionError('Existing work must not pass fresh GPU admission'))
    engine.tick();assert engine.store.get('piece-1')['state']=='running'
