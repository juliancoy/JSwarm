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
    def run(args,**kw):calls.append(args);return SimpleNamespace(stdout='{"renderD128":992}')
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
    monkeypatch.setattr(agent.subprocess,'run',lambda *a,**k:SimpleNamespace(stdout='\n'.join(json.dumps(x) for x in labels)))
    rows=agent.gpu_worker_commitments()['devices']
    assert rows['GPU-one']['exclusive_worker_present']
    assert not rows['GPU-two']['exclusive_worker_present'] and rows['GPU-two']['private_commitment_mib']==512



def test_nvidia_inventory_parses_driver_uuid_and_allocated_vram():
    row=agent.nvidia_allocation_rows('GPU-11111111-1111-1111-1111-111111111111, 00000000:04:00.0, RTX, 12288, 8192, 10, 50')[0]
    assert row['uuid'].startswith('GPU-') and row['memory_total_mib']==12288
    assert row['memory_free_mib']==8192 and row['gpu_allocation_added_to_host_ram'] is False
