"""Exercise the RAM cache lifecycle against an isolated local Docker volume."""
import hashlib
import json
import subprocess
import uuid

import pytest

from scripts import launcher_cache as module
from scripts import swarm_shared_cache as shared


def binding(image, data, *, filename='payload'):
    files={filename:{'bytes':len(data),'sha256':hashlib.sha256(data).hexdigest()}}
    key,_=shared.contract(image,files)
    size=(len(data)+1024**2-1)//1024**2+16
    return dict(image=image,files=files,key=key,layout=module.LAYOUT,container=module.NAME,volume=module.NAME,
                path='/cache/objects/'+key,subpath='objects/'+key,shm_mb=size,memory_mb=size)


@pytest.fixture
def keeper(monkeypatch):
    probe=subprocess.run(['docker','image','inspect','python:3.12-bookworm'],capture_output=True,text=True)
    if probe.returncode:pytest.skip('Local Python Docker image unavailable')
    image=json.loads(probe.stdout)[0]['Id']
    name='jswarm-cache-test-'+uuid.uuid4().hex
    monkeypatch.setattr(module,'NAME',name)
    monkeypatch.setenv('SWARM_CACHE_BUDGET_MB','64')
    manager=module.CacheManager(capacity=lambda:{'memory_mb':1024,'available_mb':512},machine='a'*64,name=name)
    try:yield manager,image
    finally:
        subprocess.run(['docker','rm','-f',name],capture_output=True)
        subprocess.run(['docker','volume','rm',name],capture_output=True)


def load(manager, cache, data):
    token=uuid.uuid4().hex*2
    result=manager.dispatch('begin',{'cache':cache,'token':token})
    if result['state']=='loading':
        manager.docker(['exec',manager.name,'python3','-c',
                        'import pathlib,sys;pathlib.Path(sys.argv[1]).write_bytes(bytes.fromhex(sys.argv[2]))',
                        cache['path']+'/payload',data.hex()])
        manager.dispatch('seal',{'key':cache['key'],'token':token})
    return result


def test_single_keeper_budget_evicts_idle_objects_and_rebuilds_misses(keeper):
    manager,image=keeper
    objects=[binding(image,bytes([i])) for i in range(4)]
    for i,cache in enumerate(objects):load(manager,cache,bytes([i]))
    inventory=manager.dispatch('inventory',{})
    assert len(inventory['items'])==3 and inventory['used_mb']<=64
    assert objects[0]['key'] not in {i['key'] for i in inventory['items']}
    assert manager.docker(['ps','-aq','--no-trunc','--filter','volume='+manager.name]).split()==[manager.owner()['Id']]
    with pytest.raises(RuntimeError,match='Cache miss'):
        manager.dispatch('check',{'key':objects[0]['key']})
    assert load(manager,objects[0],b'\x00')['state']=='loading'
    assert load(manager,objects[0],b'\x00')['state']=='ready'
    assert manager.owner()['HostConfig']['Memory']==(128+51)*1024**2


def test_loading_and_reader_pins_survive_agent_replacement_and_cleanup(keeper):
    manager,image=keeper;cache=binding(image,b'one');token='b'*64
    assert manager.dispatch('begin',{'cache':cache,'token':token})['state']=='loading'
    assert manager.dispatch('prune',{})['removed']==[]
    manager.docker(['exec',manager.name,'python3','-c',
                    'import pathlib,sys;pathlib.Path(sys.argv[1]).write_bytes(b"one")',cache['path']+'/payload'])
    manager.dispatch('seal',{'key':cache['key'],'token':token})
    manager.dispatch('pin',{'key':cache['key'],'token':token})
    successor=module.CacheManager(capacity=manager.capacity,machine=manager.machine,name=manager.name)
    assert successor.dispatch('prune',{})['removed']==[]
    successor.dispatch('release',{'key':cache['key'],'token':token})
    assert len(successor.dispatch('prune',{})['removed'])==1


def test_readonly_subpath_hides_other_objects_and_stopped_consumer_blocks_eviction(keeper):
    manager,image=keeper;first=binding(image,b'one');second=binding(image,b'two')
    load(manager,first,b'one');load(manager,second,b'two')
    reader=manager.name+'-reader'
    try:
        manager.docker(['run','--name',reader,'--network','none','--read-only','--cap-drop','ALL',
                        '--mount','type=volume,src='+manager.name+',dst=/dev/shm,readonly,volume-nocopy,volume-subpath='+first['subpath'],
                        image,'python3','-c',
                        'import pathlib; p=pathlib.Path("/dev/shm"); assert (p/"payload").read_bytes()==b"one"; assert not (p/"objects").exists();\ntry:(p/"payload").write_bytes(b"changed")\nexcept OSError:pass\nelse:raise AssertionError("Writable cache")'])
        assert manager.dispatch('drop',{'key':first['key']})['reason']=='pinned'
        assert manager.dispatch('drop',{'key':second['key']})['removed']
        manager.docker(['rm',reader])
        assert manager.dispatch('drop',{'key':first['key']})['removed']
    finally:subprocess.run(['docker','rm','-f',reader],capture_output=True)


def test_seal_rejects_modified_contents_and_foreign_paths(keeper):
    manager,image=keeper;cache=binding(image,b'expected');token='b'*64
    bad={**cache,'path':'/cache/objects/../../etc'}
    with pytest.raises(ValueError):manager.dispatch('begin',{'cache':bad,'token':token})
    manager.dispatch('begin',{'cache':cache,'token':token})
    manager.docker(['exec',manager.name,'python3','-c',
                    'import pathlib,sys;pathlib.Path(sys.argv[1]).write_bytes(b"modified")',cache['path']+'/payload'])
    with pytest.raises(RuntimeError,match='checksum differs'):
        manager.dispatch('seal',{'key':cache['key'],'token':token})
    assert manager.dispatch('inventory',{})['items'][0]['phase']=='loading'
