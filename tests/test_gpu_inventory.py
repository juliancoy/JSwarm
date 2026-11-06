import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
import time

import pytest
from scripts import launcher_agent as agent
from scripts import launcher_streams as streams


def test_amd_reserved_vram_is_separate_from_gtt_and_host_ram(tmp_path):
    values={'mem_info_vram_total':8*1024**3,'mem_info_vram_used':80*1024**2,'mem_info_gtt_total':4*1024**3,'mem_info_gtt_used':20*1024**2}
    for name,value in values.items():(tmp_path/name).write_text(str(value))
    row=agent.drm_memory_allocation(tmp_path)
    assert row['memory_total_mib']==8192 and row['memory_free_mib']==8112
    assert row['gtt_total_mib']==4096 and row['memory_domain']=='driver-reserved-vram'
    assert row['gpu_allocation_added_to_host_ram'] is False
    (tmp_path/'mem_info_vram_used').unlink()
    assert agent.drm_memory_allocation(tmp_path)['memory_free_mib'] is None


def test_hardware_inventory_discovers_render_node_and_allocated_memory(tmp_path,monkeypatch):
    device=tmp_path/'0000:04:00.0';device.mkdir();(device/'drm/renderD128').mkdir(parents=True)
    for name,value in {'class':'0x030000','vendor':'0x1002','device':'0x1636','mem_info_vram_total':str(8*1024**3),'mem_info_vram_used':'0'}.items():(device/name).write_text(value)
    monkeypatch.setattr(agent.shutil,'which',lambda _:None)
    row=agent.hardware_sample(tmp_path,tmp_path)['gpus'][0]
    assert row['render_device']=='/dev/dri/renderD128' and row['memory_total_mib']==8192
    assert row['device_id']=='0x1636'


def test_gpu_node_identity_changes_when_physical_model_changes(tmp_path,monkeypatch):
    a=agent.Agent.__new__(agent.Agent);a.root=tmp_path
    row={'pci_id':'0000:04:00.0','vendor_id':'0x1002','device_id':'0x1636','vendor':'AMD','render_device':'/dev/dri/renderD128','render_gid':992,'memory_total_mib':8192,'memory_free_mib':8000}
    monkeypatch.setattr(agent,'hardware_sample',lambda **kw:{'gpus':[row]})
    monkeypatch.setattr(agent,'gpu_worker_commitments',lambda:{'known':True,'devices':{}})
    a.host_render_groups=lambda:{}
    first=a.gpu_resources();assert first['gpus'][0]['allocation_known']
    assert 'host_memory' in first and first['host_memory']['total_mib']>0
    row['device_id']='0x9999';a.gpu_resource_cache=None
    assert a.gpu_resources()['gpus'][0]['uuid']!=first['gpus'][0]['uuid']


def test_host_render_group_probe_is_readonly_and_cached(tmp_path,monkeypatch):
    a=agent.Agent.__new__(agent.Agent);a.root=tmp_path;calls=[]
    def run(args,**kw):calls.append(args);return SimpleNamespace(returncode=0,stderr='',stdout='{"renderD128":992}')
    monkeypatch.setattr(agent.subprocess,'run',run)
    assert a.host_render_groups()=={'renderD128':992}
    assert a.host_render_groups()=={'renderD128':992} and len(calls)==1
    assert '--device' not in calls[0] and 'type=bind,src=/dev/dri,dst=/host-dri,readonly' in calls[0]


def test_binding_resolves_node_render_changes_but_never_guesses_vram():
    gpu={'uuid':'DRM-'+'a'*32,'pci_id':'0000:04:00.0','render_device':'/dev/dri/renderD128','render_gid':992,'minimum_free_mib':512,'vram_mb':256}
    row={'uuid':gpu['uuid'],'pci_id':gpu['pci_id'],'vendor':'AMD','render_device':'/dev/dri/renderD129','render_gid':993,'memory_domain':'driver-reserved-vram','allocation_known':True,'memory_free_mib':8000}
    data={'machine':'machine','sampled_at':time.time(),'gpus':[row],'host_memory':{'available_mib':100000}}
    selected=streams.resolve_gpu_allocation(gpu,data,'machine')
    assert selected['render_device']=='/dev/dri/renderD129' and selected['render_gid']==993
    assert gpu['render_gid']==992  # Immutable qualification contract retained.
    for changed in ({'allocation_known':False},{'memory_free_mib':None},{'memory_free_mib':100},{'memory_domain':'shared-system-memory'},{'pci_id':'other'}):
        with pytest.raises(RuntimeError):streams.resolve_gpu_allocation(gpu,{**data,'gpus':[{**row,**changed}]},'machine')
    with pytest.raises(RuntimeError):streams.resolve_gpu_allocation(gpu,{**data,'sampled_at':time.time()-11},'machine')
    with pytest.raises(RuntimeError):streams.resolve_gpu_allocation(gpu,data,'other')



def test_node_worker_inventory_recognizes_legacy_exclusive_jobs(monkeypatch):
    labels=[{'org.lifetech.accelerator':'GPU-one'}, {'org.lifetech.accelerator':'GPU-two','org.lifetech.gpu-concurrency':'4','org.lifetech.gpu-private-mib':'512'}]
    monkeypatch.setattr(agent.subprocess,'run',lambda *a,**k:SimpleNamespace(returncode=0,stderr='',stdout='\n'.join(json.dumps(x) for x in labels)))
    rows=agent.gpu_worker_commitments()['devices']
    assert rows['GPU-one']['exclusive_worker_present']
    assert not rows['GPU-two']['exclusive_worker_present'] and rows['GPU-two']['private_commitment_mib']==512



def test_nvidia_inventory_parses_driver_uuid_and_allocated_vram():
    row=agent.nvidia_allocation_rows('GPU-11111111-1111-1111-1111-111111111111, 00000000:04:00.0, RTX, 12288, 8192, 10, 50')[0]
    assert row['uuid'].startswith('GPU-') and row['memory_total_mib']==12288
    assert row['memory_free_mib']==8192 and row['gpu_allocation_added_to_host_ram'] is False
    assert row['pci_id']=='0000:04:00.0'


def test_nvidia_probe_timestamps_measurement_after_container_startup(tmp_path,monkeypatch):
    a=agent.Agent.__new__(agent.Agent);a.root=tmp_path
    clock=[100.0];calls=[]
    monkeypatch.setattr(agent.time,'time',lambda:clock[0])
    monkeypatch.setattr(agent,'hardware_sample',lambda **kw:{'gpus':[{'vendor':'NVIDIA'},{'vendor':'Unknown','uuid':'other-device'}]})
    monkeypatch.setattr(agent,'gpu_worker_commitments',lambda:{'known':True,'devices':{}})
    def run(args,**kwargs):
        calls.append(args)
        if args[1]=='run' and kwargs['timeout']<30:
            raise agent.subprocess.TimeoutExpired(args,kwargs['timeout'])
        clock[0]=130.0
        return SimpleNamespace(returncode=0,stderr='',stdout=json.dumps({'sampled_at':129.0,'output':'GPU-11111111-1111-1111-1111-111111111111, 00000000:04:00.0, RTX, 12288, 8192, 0, 40'}))
    monkeypatch.setattr(agent.subprocess,'run',run)
    data=a.gpu_resources()
    assert data['sampled_at']==130.0
    assert data['gpus'][0]['sampled_at']==100.0  # Unrelated older observation.
    assert data['gpus'][1]['sampled_at']==129.0
    gpu={'uuid':data['gpus'][1]['uuid'],'minimum_free_mib':512}
    assert streams.resolve_gpu_allocation(gpu,data,data['machine'])==gpu
    clock[0]=140.0
    with pytest.raises(RuntimeError,match='stale'):streams.resolve_gpu_allocation(gpu,data,data['machine'])
    assert calls[0][-3:-1]==['python3','-c']


def test_selected_device_freshness_is_independent_of_other_devices(monkeypatch):
    monkeypatch.setattr(streams.time,'time',lambda:100.0)
    gpu={'uuid':'GPU-selected','minimum_free_mib':512}
    selected={'uuid':gpu['uuid'],'sampled_at':99.0,'allocation_known':True,
              'memory_free_mib':8192,'workers_known':True}
    data={'machine':'machine','sampled_at':99.5,'gpus':[
        {'uuid':'DRM-unrelated','sampled_at':70.0},selected]}
    assert streams.resolve_gpu_allocation(gpu,data,'machine')==gpu
    for changes in ({'sampled_at':89.0},{'sampled_at':106.0},{'sampled_at':float('nan')},
                    {'workers_known':False},{'memory_free_mib':0}):
        with pytest.raises(RuntimeError):
            streams.resolve_gpu_allocation(gpu,{**data,'gpus':[{**selected,**changes}]},'machine')
    with pytest.raises(RuntimeError,match='stale'):
        streams.resolve_gpu_allocation(gpu,{**data,'sampled_at':89.0},'machine')


def test_shared_memory_freshness_is_required_even_with_fresh_device(monkeypatch):
    monkeypatch.setattr(streams.time,'time',lambda:100.0)
    gpu={'uuid':'DRM-selected','minimum_free_mib':512,'memory_domain':'shared-system-memory'}
    row={'uuid':gpu['uuid'],'sampled_at':99.0,'vendor':'Intel','device_type':'integrated',
         'memory_domain':'shared-system-memory','workers_known':True}
    data={'machine':'machine','sampled_at':99.0,'gpus':[row],
          'host_memory':{'available_mib':8192,'sampled_at':89.0}}
    with pytest.raises(RuntimeError,match='stale'):
        streams.resolve_gpu_allocation(gpu,data,'machine')
    assert streams.resolve_gpu_allocation(gpu,{**data,'host_memory':{
        'available_mib':8192,'sampled_at':99.0}},'machine')==gpu


def test_timed_out_nvidia_probe_removes_its_container(tmp_path,monkeypatch):
    a=agent.Agent.__new__(agent.Agent);calls=[]
    def run(args,**kwargs):
        calls.append(args)
        if args[1]=='run':raise agent.subprocess.TimeoutExpired(args,15)
        return SimpleNamespace(returncode=0,stderr='',stdout='')
    monkeypatch.setattr(agent.subprocess,'run',run)
    assert a.host_nvidia_allocations()==[]
    name=calls[0][calls[0].index('--name')+1]
    assert calls[1]==['docker','rm','-f',name]


def test_node_hardware_reads_fresh_native_gpu_counters_and_uses_runtime_when_needed(tmp_path,monkeypatch):
    a=agent.Agent.__new__(agent.Agent);a.root=tmp_path;a.config=tmp_path/'peers.json';a.hardware_cache=None;a.hardware_sampled=0;a.gpu_validation={}
    monkeypatch.setattr(agent,'hardware_sample',lambda **kw:{'gpus':[{'pci_id':'0000:04:00.0','vendor':'NVIDIA'}]})
    calls=[];a.host_nvidia_allocations=lambda:(calls.append(True) or [{'pci_id':'0000:04:00.0','vendor':'NVIDIA','utilization_percent':10}])
    p=tmp_path/'performance-state.json';p.write_text(json.dumps({'status':'published','sampled_at':time.time(),'gpu_devices':{'0000:04:00.0':{'utilization_percent':74}}}))
    assert a.hardware()['gpus'][0]['utilization_percent']==74
    assert a.hardware()['gpus'][0]['utilization_percent']==74 and len(calls)==0
    p.write_text(json.dumps({'status':'published','sampled_at':time.time()-30,'gpu_devices':{'0000:04:00.0':{'utilization_percent':99}}}))
    a.hardware();a.hardware_gpu_probe.join(timeout=1)
    assert a.hardware()['gpus'][0]['utilization_percent']==10
    p.write_text(json.dumps({'status':'published','sampled_at':time.time(),'gpu_devices':{'0000:04:00.0':{'utilization_percent':74}}}))
    assert a.hardware()['gpus'][0]['utilization_percent']==74


def test_hung_gpu_runtime_does_not_block_node_hardware(tmp_path,monkeypatch):
    import threading
    a=agent.Agent.__new__(agent.Agent);a.root=tmp_path;a.config=tmp_path/'peers.json';a.hardware_cache=None;a.hardware_sampled=0;a.gpu_validation={}
    monkeypatch.setattr(agent,'hardware_sample',lambda **kw:{'gpus':[{'pci_id':'pci','vendor':'NVIDIA'}]})
    started=threading.Event();release=threading.Event()
    def collect():started.set();release.wait(2);return []
    a.host_nvidia_allocations=collect
    try:
        before=time.monotonic();a.hardware()
        assert time.monotonic()-before<.5 and started.wait(1)
        before=time.monotonic();a.hardware()
        assert time.monotonic()-before<.5
    finally:release.set();a.hardware_gpu_probe.join(timeout=1)


def test_legacy_gpu_count_and_exclusivity_do_not_override_real_headroom():
    gpu={'uuid':'GPU-'+'a'*36,'minimum_free_mib':512,'vram_mb':256,'max_concurrency':1}
    row={'uuid':gpu['uuid'],'workers_known':True,'active_workers':40,'exclusive_worker_present':True,'allocation_known':True,'memory_free_mib':20000,'private_commitment_mib':10240}
    data={'machine':'machine','sampled_at':time.time(),'gpus':[row]}
    selected=streams.resolve_gpu_allocation(gpu,data,'machine')
    assert 'max_concurrency' not in selected
    with pytest.raises(RuntimeError,match='headroom'):
        streams.resolve_gpu_allocation(gpu,{**data,'gpus':[{**row,'memory_free_mib':1000}]},'machine')


def test_atomic_gpu_admission_has_no_count_cap_but_still_enforces_memory(tmp_path,monkeypatch):
    import contextlib,fcntl
    namespace={'__name__':'gpu_admission_test'};exec(streams.GPU_ADMISSION_SOURCE,namespace)
    gpu={'uuid':'GPU-'+'b'*36,'minimum_free_mib':4,'vram_mb':4,'max_concurrency':1,'lock_directory':str(tmp_path)}
    free=[8192]
    monkeypatch.setattr(namespace['subprocess'],'check_output',lambda *a,**k:gpu['uuid']+', '+str(free[0]))
    with contextlib.ExitStack() as held:
        # A legacy exclusive lock and the former maximum of 32 cannot cap work.
        legacy=held.enter_context((tmp_path/(gpu['uuid']+'.lock')).open('a'))
        fcntl.flock(legacy,fcntl.LOCK_EX|fcntl.LOCK_NB)
        for _ in range(40):held.enter_context(namespace['acquire_gpu'](gpu))
        free[0]=8
        with pytest.raises(BlockingIOError,match='headroom'):namespace['acquire_gpu'](gpu)
    free[0]=8192
    namespace['acquire_gpu'](gpu).close()


def test_nvidia_probe_timeout_cleans_up_its_unique_container(monkeypatch):
    import subprocess
    from types import SimpleNamespace
    from scripts import launcher_agent as agent
    calls=[]
    def run(args,**kwargs):
        calls.append(args)
        if args[:2]==['docker','run']:raise subprocess.TimeoutExpired(args,10)
        return SimpleNamespace(returncode=0,stderr='')
    monkeypatch.setattr(agent.subprocess,'run',run)
    a=object.__new__(agent.Agent)
    assert a.host_nvidia_allocations()==[]
    assert a.host_nvidia_allocations()==[]
    names=[args[args.index('--name')+1] for args in calls if args[:2]==['docker','run']]
    assert len(set(names))==1
    for name in names:assert ['docker','rm','-f',name] in calls


def test_integrated_intel_uses_host_headroom_without_inventing_vram():
    gpu={'uuid':'DRM-'+'c'*32,'minimum_free_mib':512,'vram_mb':256,'memory_domain':'shared-system-memory','render_device':'/dev/dri/renderD128','render_gid':110,'pci_id':'0000:00:02.0'}
    row={'uuid':gpu['uuid'],'vendor':'Intel','device_type':'integrated','memory_domain':'shared-system-memory','pci_id':gpu['pci_id'],'render_device':gpu['render_device'],'render_gid':110,'allocation_known':False,'memory_free_mib':None,'private_commitment_mib':256}
    data={'machine':'m','sampled_at':time.time(),'gpus':[row],'host_memory':{'available_mib':2048}}
    assert streams.resolve_gpu_allocation(gpu,data,'m')['memory_domain']=='shared-system-memory'
    assert streams.gpu_host_memory_mb(gpu)==256
    assert streams.gpu_host_memory_mb({**gpu,'memory_domain':'driver-reserved-vram'})==0
    for changed in ({'device_type':'discrete'},{'vendor':'AMD'},{'memory_domain':'unknown'}):
        with pytest.raises(RuntimeError):streams.resolve_gpu_allocation(gpu,{**data,'gpus':[{**row,**changed}]},'m')
    with pytest.raises(RuntimeError,match='headroom'):
        streams.resolve_gpu_allocation(gpu,{**data,'host_memory':{'available_mib':1000}},'m')
    with pytest.raises(RuntimeError):streams.resolve_gpu_allocation(gpu,{**data,'host_memory':{}},'m')


def test_intel_inventory_requires_matching_physical_integrated_device(tmp_path,monkeypatch):
    a=agent.Agent.__new__(agent.Agent);a.root=tmp_path
    row={'vendor':'Intel','vendor_id':'0x8086','device_id':'0x5917','pci_id':'0000:00:02.0','render_device':'/dev/dri/renderD128','render_gid':110,'memory_free_mib':None,'memory_total_mib':None}
    monkeypatch.setattr(agent,'hardware_sample',lambda **kw:{'gpus':[row]})
    monkeypatch.setattr(agent,'gpu_worker_commitments',lambda:{'known':True,'devices':{}})
    a.host_render_groups=lambda:{}
    a.gpu_host_icds=[{'path':'/usr/share/vulkan/icd.d/intel_icd.x86_64.json','library':'/usr/lib/x86_64-linux-gnu/libvulkan_intel.so'},{'path':'/usr/share/vulkan/icd.d/intel_hasvk_icd.x86_64.json','library':'libvulkan_intel_hasvk.so'}]
    physical={'vendor_id':'0x8086','device_id':'0x5917','integrated':True,'name':'Intel UHD 620'}
    a.host_intel_devices=lambda *args:[physical]
    selected=a.gpu_resources()['gpus'][0]
    assert selected['memory_domain']=='shared-system-memory' and selected['device_type']=='integrated'
    assert selected['memory_free_mib'] is None and selected['allocation_known'] is False
    assert len(selected['icd_candidates'])==1 and selected['shared_memory_available_mib']>0
    for change in ({'integrated':False},{'device_id':'0x9999'}):
        physical.update(change);a.gpu_resource_cache=None
        assert a.gpu_resources()['gpus'][0].get('device_type')!='integrated'


def test_intel_physical_probe_is_bounded_cached_and_reaps_helper(monkeypatch):
    a=agent.Agent.__new__(agent.Agent);calls=[]
    def run(args,**kw):
        calls.append(args)
        return SimpleNamespace(returncode=0,stderr='',stdout='[{"vendor_id":"0x8086","device_id":"0x5917","integrated":true,"name":"Intel"}]')
    monkeypatch.setattr(agent.subprocess,'run',run)
    for _ in range(2):assert a.host_intel_devices('/dev/dri/renderD128',110,'/usr/share/vulkan/icd.d/intel_icd.x86_64.json')[0]['integrated']
    assert len(calls)==1 and '--rm' in calls[0]
    assert '--read-only' in calls[0] and '--device' in calls[0] and '--memory' in calls[0]


def test_intel_guard_reads_live_host_headroom_and_preserves_atomic_commitments(tmp_path,monkeypatch):
    namespace={'__name__':'gpu_admission_test'};exec(streams.GPU_ADMISSION_SOURCE,namespace)
    sysfs=tmp_path/'sysfs';render=sysfs/'renderD128';render.mkdir(parents=True)
    device=tmp_path/'0000:00:02.0';device.mkdir();(device/'vendor').write_text('0x8086');(render/'device').symlink_to(device)
    runtime=tmp_path/'runtime';runtime.mkdir();files={}
    for name in ('bwa','exact-extension.spv'):
        (runtime/name).write_bytes(name.encode());files[name]=hashlib.sha256(name.encode()).hexdigest()
    memory=tmp_path/'meminfo';memory.write_text('MemAvailable: 1048576 kB\n')
    locks=tmp_path/'locks';locks.mkdir()
    namespace['pathlib']=SimpleNamespace(Path=lambda p:sysfs if p=='/sys/class/drm' else runtime if p=='/gpu-runtime' else memory if p=='/proc/meminfo' else Path(p))
    gpu={'uuid':'DRM-'+'d'*32,'pci_id':device.name,'render_device':'/dev/dri/renderD128','memory_domain':'shared-system-memory','runtime_files':files,'lock_directory':str(locks),'minimum_free_mib':512,'vram_mb':256}
    first=namespace['acquire_gpu'](gpu)
    try:
        second=namespace['acquire_gpu'](gpu);second.close()
        memory.write_text('MemAvailable: 921600 kB\n')
        with pytest.raises(BlockingIOError,match='Host-backed'):namespace['acquire_gpu'](gpu)
    finally:first.close()
    namespace['acquire_gpu'](gpu).close()


def test_unsupported_nvidia_runtime_never_creates_helpers(monkeypatch):
    a=agent.Agent.__new__(agent.Agent);a.cuda_runtime=False
    monkeypatch.setattr(agent.subprocess,'run',lambda *a,**k:pytest.fail('unsupported runtime launched a helper'))
    assert a.host_nvidia_allocations()==[]


def test_failed_probe_cleanup_blocks_further_container_creation(monkeypatch):
    a=agent.Agent.__new__(agent.Agent);calls=[]
    def run(args,**kwargs):
        calls.append(args)
        if args[1]=='run':raise agent.subprocess.TimeoutExpired(args,15)
        raise agent.subprocess.TimeoutExpired(args,30)
    monkeypatch.setattr(agent.subprocess,'run',run)
    for _ in range(3):
        a.nvidia_probe_retry_at=0
        assert a.host_nvidia_allocations()==[]
    assert len([args for args in calls if args[1]=='run'])==1
    assert len([args for args in calls if args[1]=='rm'])==3
    assert a.gpu_probe_pending==calls[0][3]


def test_failed_nvidia_probe_retries_after_backoff(monkeypatch):
    a=agent.Agent.__new__(agent.Agent);clock=[100.];calls=[]
    monkeypatch.setattr(agent.time,'monotonic',lambda:clock[0])
    def run(args,**kw):
        calls.append(args)
        if args[1]=='run':raise agent.subprocess.CalledProcessError(1,args)
        return SimpleNamespace(returncode=0,stderr='')
    monkeypatch.setattr(agent.subprocess,'run',run)
    assert a.host_nvidia_allocations()==[]
    clock[0]=159.;assert a.host_nvidia_allocations()==[]
    assert len(calls)==2
    clock[0]=161.;assert a.host_nvidia_allocations()==[]
    assert len(calls)==4
