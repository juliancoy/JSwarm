"""Migration preserves immutable inputs before rebinding and retirement."""
import hashlib
import io
import json
import subprocess
import sys
import tarfile
from unittest.mock import Mock

import pytest
from scripts import swarm_cache_migrate as migration
from scripts import swarm_shared_cache as caches


def legacy(data=b'preserved'):
    image='sha256:'+'a'*64
    files={'payload':{'bytes':len(data),'sha256':hashlib.sha256(data).hexdigest()}}
    key,value=caches.contract(image,files)
    return {**value,'key':key,'host':'node.local','container':'swarm-job-'+key[:32],
            'volume':'swarm-cache-'+key[:32],'shm_mb':17,'memory_mb':145}


def test_rebind_nested_queue_and_source_records_preserves_execution_identity():
    old=legacy();new=migration.managed_binding({'host':'node.local'},old)
    value={'worker_host':'node.local','token':'same-attempt','resources':{'memory_mb':1024},
           'transport':{'cache':old,'references':{'/reference':old},'work':'/dev/shm/job'},'source_caches':[old]}
    changed=migration.rebind(value,{('node.local',old['key']):new})
    assert changed['token']==value['token'] and changed['resources']==value['resources']
    assert changed['transport']['work']==value['transport']['work']
    assert all(caches.is_managed(c) for _,c in migration.bindings(changed))
    assert all(c['files']==old['files'] for _,c in migration.bindings(changed))
    assert value['transport']['cache']==old
    caches.validate_binding(new)


def test_physical_node_fences_same_key_on_other_machine():
    old=legacy();other={**old,'host':'other.local'}
    new=migration.managed_binding({'host':'node.local'},old)
    assert migration.rebind(other,{('node.local',old['key']):new})==other
    assert list(migration.bindings({**old,'host':'node.local#gpu'}))[0][0]=='node.local'
    assert migration.rebind({'cache':{k:v for k,v in old.items() if k!='host'}},
                            {('node.local',old['key']):new},'node.local')['cache']==new


def test_checksum_drift_rejected_before_durable_rebinding():
    old=legacy();new=migration.managed_binding({'host':'node.local'},old)
    changed={**new,'files':{'payload':{'bytes':1,'sha256':'0'*64}}}
    with pytest.raises(ValueError,match='Replacement differs'):
        migration.rebind(old,{('node.local',old['key']):changed})


def test_reference_scan_covers_queue_and_derived_lookup():
    old=legacy();second=legacy(b'reusable reference')
    rows=[{'host':'node.local','request':json.dumps({'cache':old})},{'host':None,'request':None}]
    refs=migration.references(rows,{'derived.json':{'identity':{'ref':1},'cache':second}})
    assert set(refs)=={('node.local',old['key']),('node.local',second['key'])}


def archive(name,data):
    stream=io.BytesIO()
    with tarfile.open(fileobj=stream,mode='w') as out:
        member=tarfile.TarInfo(name);member.size=len(data);out.addfile(member,io.BytesIO(data))
    return stream.getvalue()


@pytest.mark.parametrize('name,data,success', [('payload',b'preserved',True),
    ('payload',b'tampered!',False),('../escape',b'preserved',False),('extra',b'preserved',False)])
def test_stream_receiver_validates_payload_and_names(tmp_path,name,data,success):
    result=subprocess.run([sys.executable,'-c',migration.RECEIVE,str(tmp_path),json.dumps(legacy()['files'])],
                          input=archive(name,data),capture_output=True)
    assert (result.returncode==0)==success
    if success:assert (tmp_path/'payload').read_bytes()==b'preserved'
    assert not (tmp_path.parent/'escape').exists()


def test_apply_restarts_controller_and_releases_lock_on_failure(tmp_path,monkeypatch):
    lock=tmp_path/'genomics-followup/plan/controller.lock';lock.parent.mkdir(parents=True);lock.touch()
    calls=[]
    def run(argv,**kwargs):
        calls.append(argv);return Mock(stdout='active\n')
    monkeypatch.setattr(migration.subprocess,'run',run)
    with pytest.raises(RuntimeError,match='failed copy'):
        with migration.quiesce(tmp_path,True):
            with lock.open('a') as other:
                with pytest.raises(BlockingIOError):migration.fcntl.flock(other,migration.fcntl.LOCK_EX|migration.fcntl.LOCK_NB)
            raise RuntimeError('failed copy')
    assert [c[-2:] for c in calls]==[['is-active','genomics-followup.service'],['stop','genomics-followup.service'],['start','genomics-followup.service']]
    with lock.open('a') as stream:migration.fcntl.flock(stream,migration.fcntl.LOCK_EX|migration.fcntl.LOCK_NB)


def test_preview_never_stops_services(tmp_path,monkeypatch):
    monkeypatch.setattr(migration.subprocess,'run',lambda *a,**kw:pytest.fail('Preview mutated service state'))
    with migration.quiesce(tmp_path,False):pass


def test_docker_copy_seal_and_retirement_preserve_readers(monkeypatch):
    """Real tmpfs copy + target checksum seal precede exact-owner retirement."""
    import uuid
    from pathlib import Path
    from scripts import launcher_cache
    def docker(*args):
        return subprocess.run(['docker',*args],capture_output=True,text=True,check=True).stdout
    probe=subprocess.run(['docker','image','inspect','python:3.12-bookworm'],capture_output=True,text=True)
    if probe.returncode:pytest.skip('Local Python Docker image unavailable')
    image=json.loads(probe.stdout)[0]['Id'];data=uuid.uuid4().bytes
    old=legacy(data);old['image']=image
    key,value=caches.contract(image,old['files'])
    name='jswarm-migration-test-'+uuid.uuid4().hex
    # Isolate integration fixtures from real fleet legacy-cache discovery.
    old.update(value,key=key,container=name+'-legacy-'+key[:32],volume=name+'-volume-'+key[:32])
    retire_runtime=migration.RETIRE.replace(
        "name='swarm-job-'+key[:32];volume='swarm-cache-'+key[:32]",
        'name='+repr(name+'-legacy-')+'+key[:32];volume='+repr(name+'-volume-')+'+key[:32]')
    reader=name+'-reader'
    monkeypatch.setattr(launcher_cache,'NAME',name)
    monkeypatch.setenv('SWARM_CACHE_BUDGET_MB','64')
    manager=launcher_cache.CacheManager(capacity=lambda:{'memory_mb':1024,'available_mb':512},machine='b'*64,name=name)
    target={**migration.managed_binding({'host':'node.local'},old),'container':name,'volume':name}
    try:
        docker('volume','create','--label',caches.OWNER+'='+key,'--opt','type=tmpfs','--opt','device=tmpfs',
               '--opt','o=size=17m,mode=1777',old['volume'])
        owner=docker('run','-d','--name',old['container'],'--network','none','--label',caches.OWNER+'='+key,
               '--label','org.lifetech.group=shared-cache','--mount','type=volume,src='+old['volume']+',dst=/dev/shm',
               image,'python3','-c','import time;time.sleep(300)').strip()
        docker('exec',old['container'],'python3','-c','import pathlib,sys;pathlib.Path("/dev/shm/payload").write_bytes(bytes.fromhex(sys.argv[1]))',data.hex())
        manager.dispatch('begin',{'cache':target,'token':'c'*64})
        subprocess.run([sys.executable,'-c',migration.COPY,json.dumps({'source':old,'target':target,'receive':migration.RECEIVE})],check=True,capture_output=True)
        manager.dispatch('seal',{'key':key,'token':'c'*64})
        assert manager.dispatch('check',{'key':key})['cache']['files']==old['files']
        docker('create','--name',reader,'--mount','type=volume,src='+old['volume']+',dst=/input,readonly',image,'true')
        request={'machine':hashlib.sha256(Path('/etc/machine-id').read_bytes()).hexdigest(),'items':[{'key':key,'owner_id':owner}]}
        def retire():
            return json.loads(subprocess.check_output([sys.executable,'-c',retire_runtime,json.dumps(request)],text=True))[0]
        assert retire()['reason']=='docker_consumer'
        docker('rm',reader)
        assert retire()['removed'] is True
        assert manager.dispatch('check',{'key':key})['cache']['files']==old['files']
    finally:
        for container in (reader,old['container'],name):subprocess.run(['docker','rm','-f',container],capture_output=True)
        for volume in (old['volume'],name):subprocess.run(['docker','volume','rm',volume],capture_output=True)


def test_full_preview_uses_only_read_rpcs_and_inherits_local_derived_owner(tmp_path,monkeypatch):
    import sqlite3
    state=tmp_path/'state';folder=state/'submission/shared-objects';folder.mkdir(parents=True)
    with sqlite3.connect(state/'submission/queue.sqlite') as db:
        db.execute('CREATE TABLE submissions(namespace,id,state,host,request)')
    old=legacy();new=migration.managed_binding({'host':'node.local'},old)
    del new['host']
    (folder/'derived.json').write_text(json.dumps({'identity':{'ref':1},'cache':new}))
    conf=tmp_path/'peers.json';conf.write_text(json.dumps({'name':'node.local','peers':{'node.local':'10.0.0.1'}}))
    monkeypatch.setattr(migration,'CONFIG',conf)
    class Client:
        def __init__(self,host):pass
        def close(self):pass
        def call(self,op,args=None,**kwargs):
            if op=='status':return {'node':'node.local','machine':'a'*64,'capabilities':['node-cache-v1']}
            if op=='resource.capacity':return {'available_mb':512}
            if op=='resource.cache.inventory':return {'budget_mb':256,'items':[{**new,'phase':'ready'}]}
            pytest.fail('Preview invoked mutation: '+op)
    monkeypatch.setattr(migration,'AgentClient',Client)
    monkeypatch.setattr(migration.subprocess,'run',lambda *a,**kw:pytest.fail('Preview changed services'))
    result=migration.migrate(state,output=tmp_path/'preview.json')
    assert result['status']=='preview' and result['required_legacy_objects']==0


def test_missing_pending_source_aborts_before_any_retirement(tmp_path,monkeypatch):
    import sqlite3
    state=tmp_path/'state';(state/'submission').mkdir(parents=True)
    old=legacy();request=json.dumps({'transport':{'cache':old}})
    with sqlite3.connect(state/'submission/queue.sqlite') as db:
        db.execute('CREATE TABLE submissions(namespace,id,state,host,request)')
        db.execute('INSERT INTO submissions VALUES(?,?,?,?,?)',('plan','job','accepted','node.local',request))
    conf=tmp_path/'peers.json';conf.write_text(json.dumps({'name':'node.local','peers':{'node.local':'10.0.0.1'}}))
    monkeypatch.setattr(migration,'CONFIG',conf)
    class Client:
        def __init__(self,host):pass
        def close(self):pass
        def call(self,op,args=None,**kwargs):
            if op=='status':return {'node':'node.local','machine':'a'*64,'capabilities':['node-cache-v1']}
            if op=='resource.capacity':return {'available_mb':512}
            if op=='resource.cache.check':raise RuntimeError('Cache miss')
            pytest.fail('Unverified source reached mutation or retirement: '+op)
    monkeypatch.setattr(migration,'AgentClient',Client)
    monkeypatch.setattr(caches,'inspect',lambda *a:None)
    monkeypatch.setattr(caches,'command',lambda *a,**kw:pytest.fail('Unverified source reached node mutation'))
    calls=[]
    def service(argv,**kwargs):calls.append(argv);return Mock(stdout='active\n')
    monkeypatch.setattr(migration.subprocess,'run',service)
    output=tmp_path/'report.json'
    with pytest.raises(ValueError,match='no verified source'):
        migration.migrate(state,apply=True,output=output)
    assert calls[-1][-2:]==['start','genomics-followup.service']
    assert json.loads(output.read_text())['status']=='error'
    with sqlite3.connect(state/'submission/queue.sqlite') as db:
        assert db.execute('SELECT state,request FROM submissions').fetchone()==('accepted',request)


def test_rebound_keys_no_longer_protect_legacy_holders():
    old=legacy();new=migration.managed_binding({'host':'node.local'},old)
    rows=[{'host':'node.local','request':json.dumps({'cache':new})}]
    assert migration.legacy_references(rows,{'reference':{'cache':new}})=={}
    # Interrupted rebinding may have updated a derived lookup before the queue.
    rows[0]['request']=json.dumps({'cache':old})
    refs=migration.legacy_references(rows,{'reference':{'cache':new}})
    assert refs[('node.local',old['key'])]==old
    assert migration.rebind(json.loads(rows[0]['request']),{('node.local',old['key']):new})['cache']==new
