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
    assert manager.dispatch('begin',{'cache':cache,'token':'c'*64})['state']=='waiting'
    manager.docker(['exec',manager.name,'python3','-c',
                    'import pathlib,sys;pathlib.Path(sys.argv[1]).write_bytes(b"one")',cache['path']+'/payload'])
    manager.dispatch('seal',{'key':cache['key'],'token':token})
    manager.dispatch('pin',{'key':cache['key'],'token':token})
    successor=module.CacheManager(capacity=manager.capacity,machine=manager.machine,name=manager.name)
    assert successor.dispatch('prune',{})['removed']==[]
    successor.dispatch('release',{'key':cache['key'],'token':token})
    assert successor.dispatch('prune',{'keep':[cache['key']]})['removed']==[]
    assert len(successor.dispatch('prune',{})['removed'])==1
    load(successor,cache,b'one')
    successor.dispatch('pin',{'key':cache['key'],'token':token})
    successor.docker(['exec',successor.name,'python3','-c',
        'import json,pathlib; p=pathlib.Path("/cache/.state.json"); v=json.loads(p.read_text()); '
        '[i.update(pins={k:0 for k in i["pins"]}) for i in v.values()]; p.write_text(json.dumps(v))'])
    assert len(successor.dispatch('prune',{})['removed'])==1  # Dead reader pins cannot leak forever.


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


def test_managed_client_reuses_objects_and_pins_runtime_until_failure_cleanup(monkeypatch, tmp_path):
    from scripts import swarm_runtime as runtime
    image='sha256:'+'e'*64;data=b'input'
    record=binding(image,data);events=[];objects={};loads=[]
    class Client:
        def __init__(self,host):pass
        def close(self):pass
        def call(self,op,args=None,**kwargs):
            events.append((op,args))
            if op=='status':return {'capabilities':['node-cache-v1'],'machine':'m'}
            assert args['machine']=='m'
            if op=='resource.cache.begin':
                old=objects.get(args['cache']['key'])
                objects[args['cache']['key']]=args['cache']
                return {'state':'ready' if old else 'loading'}
            return {}
    monkeypatch.setattr(shared,'AgentClient',Client)
    monkeypatch.setattr(shared,'command',lambda *a,**kw:pytest.fail('Managed cache used legacy Docker ownership'))
    host={'host':'test','machine':'m'}
    def loader(owner,path):loads.append((owner,path))
    first=shared.ensure(host,image,record['files'],loader,load_path=True)
    assert shared.ensure(host,image,record['files'],loader,load_path=True)==first
    assert loads==[('swarm-node-cache','/cache/objects/'+record['key'])]
    def execute(*args,**kwargs):
        assert events[-1][0]=='resource.cache.pin'
        raise RuntimeError('Execution interrupted')
    monkeypatch.setattr(runtime,'_run_job',execute)
    with pytest.raises(RuntimeError,match='Execution interrupted'):
        runtime.run_job(host,image,'true',[],str(tmp_path),1,128,tmp_path/'report',group='test',shared_cache=first)
    assert events[-1][0]=='resource.cache.release'


def test_cache_rpc_rejects_replaced_machine_before_docker():
    from scripts import launcher_agent
    agent=launcher_agent.Agent.__new__(launcher_agent.Agent)
    with pytest.raises(ValueError,match='machine identity changed'):
        agent.dispatch('resource.cache.prune',{'machine':'wrong'})


def test_derived_legacy_lookup_preserves_a_valid_copy_without_rebuilding(tmp_path,monkeypatch):
    image='sha256:'+'e'*64;identity={'reference':'original'};old=binding(image,b'input')
    old={k:v for k,v in old.items() if k not in ('layout','path','subpath')}
    old.update(container='swarm-job-'+old['key'][:32],volume='swarm-cache-'+old['key'][:32])
    derived=hashlib.sha256(json.dumps({'identity':identity,'image':image},sort_keys=True,separators=(',',':')).encode()).hexdigest()
    folder=tmp_path/'.local/state/swarm-control/submission/shared-objects';folder.mkdir(parents=True)
    (folder/(derived+'.json')).write_text(json.dumps({'identity':identity,'cache':old}))
    monkeypatch.setattr(shared.Path,'home',lambda:tmp_path)
    monkeypatch.setattr(shared,'inspect',lambda *a:{'Id':'owner'})
    monkeypatch.setattr(shared,'ready',lambda *a:True)
    def unavailable(*a,**kw):pytest.fail('Valid legacy data cannot require retired staging')
    monkeypatch.setattr(shared,'ensure',unavailable)
    result=shared.ensure_derived({'host':'source','local':True,'capabilities':['node-cache-v1']},image,identity,unavailable,unavailable,load_path=True)
    assert result['cache']==old


def test_warm_keeper_cannot_lend_memory_promised_to_new_objects():
    import time
    from scripts.launcher_agent import memory_budget
    now=time.monotonic()
    limits={'memory_mb':8192,'available_mb':4096,'memory_pressure_some_percent':0,'memory_pressure_full_percent':0}
    worker={'memory_mb':2048,'resident_mb':128,'cache_keeper':True,
            'memory_observation':{'used':128,'first':now-100,'time':now,'peak':128,'growth':0}}
    result=memory_budget(limits,[worker])
    assert result['memory_growth_mb']==1920
    assert result['available_mb']==4096-result['memory_safety_mb']-1920


def test_cache_begin_holds_shared_admission_lock_until_commitment_is_visible(tmp_path,monkeypatch):
    import fcntl
    import threading
    from pathlib import Path
    from types import SimpleNamespace
    from scripts import launcher_agent as module
    machine=hashlib.sha256(Path('/etc/machine-id').read_bytes()).hexdigest()
    original_capacity=lambda:{'memory_mb':1024,'available_mb':512}
    manager=SimpleNamespace(lock=threading.RLock(),capacity=original_capacity)
    agent=module.Agent.__new__(module.Agent);agent.root=tmp_path;agent.cache_manager=manager
    monkeypatch.delenv('SWARM_RESOURCE_DIR',raising=False)
    def capacity(**kwargs):
        assert kwargs['locked'] is True
        with (tmp_path/'.resources/pool.lock').open('a') as other:
            with pytest.raises(BlockingIOError):fcntl.flock(other,fcntl.LOCK_EX|fcntl.LOCK_NB)
        return {'available_mb':512}
    monkeypatch.setattr(module.ResourceLease,'pool_capacity',capacity)
    def begin(op,args):
        assert op=='begin' and manager.capacity()['available_mb']==512
        return {'state':'loading'}
    manager.dispatch=begin
    assert agent.dispatch('resource.cache.begin',{'machine':machine})['state']=='loading'
    assert manager.capacity is original_capacity
    with (tmp_path/'.resources/pool.lock').open('a') as other:
        fcntl.flock(other,fcntl.LOCK_EX|fcntl.LOCK_NB)
