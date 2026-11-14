import hashlib
import json
from pathlib import Path
import random
import subprocess
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from scripts import swarm_shared_cache as cache

IMAGE='sha256:2817b8f8ff61d06d2e260007d79a51c676e22eb4e7b619a9c336422db039412e'


def test_contract_uses_content_not_source_path_and_rejects_unsafe_names():
    row={'bytes':4,'sha256':hashlib.sha256(b'data').hexdigest(),'path':'/one'}
    assert cache.contract(IMAGE,{'index':row})==cache.contract(IMAGE,{'index':{**row,'path':'/two'}})
    with pytest.raises(ValueError):cache.contract(IMAGE,{'../index':row})
    with pytest.raises(ValueError):cache.contract('latest',{'index':row})


@pytest.fixture
def admitted(monkeypatch,tmp_path):
    calls=[]
    class Client:
        def __init__(self,host):self.host=host
        def call(self,op,args=None):
            calls.append((op,args))
            if op=='status':return {'machine':self.host['machine'],'worker_policy':{'accepting_jobs':True}}
            return {'acquired':True}
        def close(self):pass
    monkeypatch.setattr(cache,'AgentClient',Client)
    monkeypatch.setattr(cache.Path,'home',lambda:tmp_path)
    original_command=cache.command;test_label='org.lifetech.test-run='+uuid.uuid4().hex
    def isolated_command(host,args,**kwargs):
        # Real Docker checks must never enumerate production cache owners.
        if args[:3]==['docker','ps','-aq']:
            args=[*args,'--filter','label='+test_label]
        if args[:2]==['docker','run']:
            args=[*args[:2],'--label',test_label,*args[2:]]
        return original_command(host,args,**kwargs)
    monkeypatch.setattr(cache,'command',isolated_command)
    return calls


def docker(*args,**kwargs):
    kwargs.setdefault('timeout',60)
    return subprocess.run(['docker',*args],capture_output=True,**kwargs)


@pytest.fixture
def tool_image():
    if docker('image','inspect',IMAGE).returncode:pytest.skip('Approved BWA image unavailable locally')
    return IMAGE


def test_real_concurrent_bwa_shards_share_one_readonly_inode_and_load_once(tmp_path,admitted,tool_image):
    rng=random.Random(uuid.uuid4().int);sequence=''.join(rng.choices('ACGT',k=20000))
    (tmp_path/'reference.fa').write_text('>fixture\n'+sequence+'\n')
    complement=str.maketrans('ACGT','TGCA')
    for mate in (1,2):
        reads=[]
        for i in range(60):
            start=100+i*100;read=sequence[start:start+100] if mate==1 else sequence[start+200:start+300].translate(complement)[::-1]
            reads.append('@pair'+str(i)+'/'+str(mate)+'\n'+read+'\n+\n'+'I'*100+'\n')
        (tmp_path/('r'+str(mate)+'.fq')).write_text(''.join(reads))
    (tmp_path/'objects').mkdir()
    setup='bwa index /work/reference.fa && bwa shm -f /work/pack /work/reference.fa && cp /dev/shm/bwactl /work/objects/bwactl && cp /dev/shm/bwaidx-reference.fa /work/objects/bwaidx-reference.fa && bwa shm -d && bwa mem -K 1000000 -t 1 /work/reference.fa /work/r1.fq /work/r2.fq > /work/plain.sam'
    setup_name='shared-cache-setup-'+uuid.uuid4().hex[:12]
    try:
        p=docker('run','--rm','--name',setup_name,'--network','none','--cpus','1','--memory','256m','--memory-swap','256m','--shm-size','32m','--mount','type=bind,src='+str(tmp_path)+',dst=/work',tool_image,'bash','-o','pipefail','-c',setup,timeout=180)
    finally:docker('rm','-f',setup_name)
    assert p.returncode==0,p.stderr.decode()
    files={p.name:{'bytes':p.stat().st_size,'sha256':hashlib.sha256(p.read_bytes()).hexdigest()} for p in (tmp_path/'objects').iterdir()}
    host={'host':'fixture','local':True,'machine':uuid.uuid4().hex,'container_user':'1000:1000'}
    loads=[];names=[];binding=None
    def load(container):
        loads.append(container)
        for name in files:
            data=(tmp_path/'objects'/name).read_bytes()
            result=docker('exec','-i',container,'python3','-c','import pathlib,sys;pathlib.Path(sys.argv[1]).write_bytes(sys.stdin.buffer.read())','/dev/shm/'+name,input=data,timeout=15)
            assert result.returncode==0,result.stderr.decode()
    try:
        def prepare(_):
            deadline=time.monotonic()+60
            while True:
                try:return cache.ensure(host,tool_image,files,load)
                except cache.CapacityWait:
                    assert time.monotonic()<deadline
                    time.sleep(.05)
        with ThreadPoolExecutor(max_workers=2) as pool:
            a,b=list(pool.map(prepare,range(2)))
        binding=a
        assert a==b and len(loads)==1
        assert cache.inspect(host,'container',binding['container'])['HostConfig']['NanoCpus']==100000000
        assert {r['id'] for op,r in admitted if op=='resource.acquire'}=={binding['key'][:32]}
        program=r'''
import errno,hashlib,json,os,pathlib,subprocess,time
p=pathlib.Path('/dev/shm/bwaidx-reference.fa');s=p.stat()
try:p.open('wb');raise RuntimeError('Shared reference was writable')
except OSError as e:
 if e.errno!=errno.EROFS:raise
r=subprocess.run(['bwa','mem','-K','1000000','-t','1','/ref/reference.fa','/input/r1.fq','/input/r2.fq'],capture_output=True,check=True)
data=b'\n'.join(l for l in r.stdout.splitlines() if not l.startswith(b'@'))
print(json.dumps({'inode':[s.st_dev,s.st_ino],'sam_sha256':hashlib.sha256(data).hexdigest(),'shared_memory_observed':b'shared memory' in r.stderr}))
'''
        def shard(i):
            name='shared-cache-test-'+uuid.uuid4().hex[:12];names.append(name)
            return docker('run','--rm','--name',name,'--network','none','--read-only','--cap-drop','ALL','--cpus','1','--memory','256m','--memory-swap','256m','--user','1000:1000',
                          '--mount','type=bind,src='+str(tmp_path)+',dst=/input,readonly',*cache.mount_args(host,binding),tool_image,'python3','-c',program,timeout=180)
        with ThreadPoolExecutor(max_workers=2) as pool:
            # Concurrent readers need no controller rendezvous. Active-reader
            # eviction protection is covered separately; a startup barrier here
            # only turns slow Docker creation into a deadlock.
            results=list(pool.map(shard,range(2)))
        for result in results:assert result.returncode==0,result.stderr.decode()
        results=[json.loads(r.stdout) for r in results]
        baseline=b'\n'.join(l for l in (tmp_path/'plain.sam').read_bytes().splitlines() if not l.startswith(b'@'))
        assert results[0]['inode']==results[1]['inode']
        assert all(r['shared_memory_observed'] and r['sam_sha256']==hashlib.sha256(baseline).hexdigest() for r in results)
        assert cache.ensure(host,tool_image,files,lambda _:pytest.fail('Reloaded warm cache'))==binding
        # An authorized mutation must invalidate the proof rather than silently reuse data.
        assert docker('exec',binding['container'],'python3','-c','import pathlib;p=pathlib.Path("/dev/shm/bwactl");p.chmod(0o644);p.write_bytes(b"changed")').returncode==0
        with pytest.raises(RuntimeError,match='Cache object changed'):cache.mount_args(host,binding)
    finally:
        for name in names:docker('rm','-f',name)
        if binding:cache._remove_unreferenced(host,binding)


def test_slow_daemon_cleanup_is_deferred(monkeypatch):
    monkeypatch.setattr(cache,'users',lambda *args:[])
    monkeypatch.setattr(cache,'inspect',lambda *args:{'Id':'owned-cache'})
    def slow(*args,**kwargs):raise subprocess.TimeoutExpired('docker rm',45)
    monkeypatch.setattr(cache,'command',slow)
    assert cache._remove_unreferenced({}, {'container':'cache','volume':'cache-volume'}) is False


def test_corrupt_cache_is_rejected_and_removed(admitted,tool_image):
    host={'host':'fixture','local':True,'machine':uuid.uuid4().hex}
    expected=uuid.uuid4().bytes
    files={'index':{'bytes':len(expected),'sha256':hashlib.sha256(expected).hexdigest()}}
    key,_=cache.contract(tool_image,files)
    def load(container):
        assert docker('exec',container,'python3','-c','import pathlib;pathlib.Path("/dev/shm/index").write_bytes(b"evil")').returncode==0
    with pytest.raises(RuntimeError,match='checksum mismatch'):cache.ensure(host,tool_image,files,load)
    assert cache.inspect(host,'container','swarm-job-'+key[:32]) is None
    assert cache.inspect(host,'volume','swarm-cache-'+key[:32]) is None


def test_stopped_ram_cache_is_reloaded_instead_of_restarted_empty(tmp_path,admitted,tool_image):
    import os
    host={'host':'fixture','local':True,'machine':uuid.uuid4().hex,'container_user':str(os.getuid())+':'+str(os.getgid())}
    payload=uuid.uuid4().bytes
    files={'index':{'bytes':len(payload),'sha256':hashlib.sha256(payload).hexdigest()}}
    loads=[];binding=None
    def load(name):
        loads.append(name)
        r=docker('exec','-i',name,'python3','-c','import pathlib,sys;pathlib.Path("/dev/shm/index").write_bytes(sys.stdin.buffer.read())',input=payload,timeout=15)
        assert r.returncode==0,r.stderr.decode()
    try:
        binding=cache.ensure(host,tool_image,files,load)
        first=cache.inspect(host,'container',binding['container'])
        assert first['HostConfig']['RestartPolicy']['Name']=='no'
        assert docker('stop','-t','1',binding['container'],timeout=60).returncode==0
        rebuilt=cache.ensure(host,tool_image,files,load)
        second=cache.inspect(host,'container',rebuilt['container'])
        assert second['Id']!=first['Id'] and len(loads)==2
        assert cache.ready(host,rebuilt,second)
    finally:
        if binding:cache._remove_unreferenced(host,binding)


def test_concurrent_derivations_publish_one_cache_and_mount_readonly_targets(tmp_path,admitted,tool_image):
    host={'host':'fixture','local':True,'machine':uuid.uuid4().hex,'container_user':'1000:1000'}
    builds=[];loads=[];binding=None;payload=uuid.uuid4().bytes
    reader='shared-cache-derived-reader-'+uuid.uuid4().hex[:12]
    def build(root):builds.append(1);(root/'reference.fa').write_bytes(payload)
    def load(owner,root,files):
        loads.append(owner)
        p=docker('exec','-i',owner,'python3','-c',"import pathlib,sys;pathlib.Path('/dev/shm/reference.fa').write_bytes(sys.stdin.buffer.read())",input=(root/'reference.fa').read_bytes())
        assert p.returncode==0,p.stderr.decode()
    try:
        def prepare(_):
            deadline=time.monotonic()+60
            while True:
                try:return cache.ensure_derived(host,tool_image,{'source':'sha','contig':'1'},build,load)
                except cache.CapacityWait:
                    assert time.monotonic()<deadline
                    time.sleep(.05)
        with ThreadPoolExecutor(2) as pool:results=list(pool.map(prepare,range(2)))
        binding=results[0]['cache'];assert results[0]==results[1] and len(loads)==1
        count=len(builds)
        assert cache.ensure_derived(host,tool_image,{'source':'sha','contig':'1'},build,load)==results[0]
        assert len(builds)==count
        args=cache.mount_args(host,binding,target='/references/one',primary=False)
        # Match the neighboring BWA integration test's bounded startup budget;
        # a busy local daemon may spend over a minute creating the image mount.
        p=docker('run','--rm','--name',reader,'--network','none',*args,tool_image,'python3','-c',"import pathlib;assert pathlib.Path('/references/one/reference.fa').read_bytes()=="+repr(payload),timeout=180)
        assert p.returncode==0,p.stderr.decode()
        assert 'dst=/references/one,readonly' in ' '.join(args)
        with pytest.raises(ValueError):cache.mount_args(host,binding,target='/')
        # A live bulk reader must keep its source cache from eviction.
        subprocess.run(['docker','exec','-d',binding['container'],'python3','-c','import time;time.sleep(3)'],check=True)
        assert cache.remove_unused(host,binding) is False
        time.sleep(3.2)
    finally:
        docker('rm','-f',reader)
        if binding:cache._remove_unreferenced(host,binding)


def test_unscoped_pressure_sweep_cannot_evict_prepared_queue_inputs(monkeypatch):
    monkeypatch.setattr(cache,'command',lambda *a,**k:pytest.fail('Unscoped eviction enumerated caches without queue ownership'))
    assert cache.evict_unused({'host':'fixture'},keep='other-object')==0


def test_finished_cache_keys_protects_unfinished_claims_and_other_nodes(tmp_path):
    import sqlite3
    path=tmp_path/'queue.sqlite'
    def binding(key):return {'key':key,'container':'swarm-job-'+key[:32],'volume':'swarm-cache-'+key[:32]}
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE submissions(state TEXT,host TEXT,request TEXT)')
        for state,host,keys in [('completed','mini',['a'*64,'b'*64]),
                                ('accepted','mini',['b'*64]),('preparing','mini#cpu',['c'*64]),
                                ('completed','mini',['c'*64]),('completed','other',['d'*64])]:
            db.execute('INSERT INTO submissions VALUES(?,?,?)',(state,host,json.dumps({'transport':{'references':[binding(k) for k in keys]}})))
    assert cache.finished_cache_keys({'host':'mini'},path)=={'a'*64}
    assert cache.finished_cache_keys({'host':'mini'},tmp_path/'missing')==set()


def test_reclamation_requires_owner_candidates_and_preserves_live_bulk_readers(monkeypatch):
    monkeypatch.setattr(cache,'command',lambda *a,**kw:pytest.fail('Unscoped eviction queried Docker'))
    assert cache.evict_unused({'host':'mini'})==0
    monkeypatch.setattr(cache,'users',lambda *a:[])
    monkeypatch.setattr(cache,'inspect',lambda *a:{'Id':'owner-id','ExecIDs':['active-reader']})
    assert cache.remove_unused({'host':'mini'},{'container':'cache','volume':'cache-volume'}) is False


def test_placement_reclaims_before_rejecting_idle_node(monkeypatch):
    from unittest.mock import Mock
    client=Mock();client.call.side_effect=[{'available_mb':936},{'available_mb':2200}]
    reclaim=Mock(return_value=3);monkeypatch.setattr(cache,'reclaim_completed',reclaim)
    host={'host':'mini','machine':'id'}
    assert cache.capacity_for_work(host,client,'compute',1792)['available_mb']==2200
    reclaim.assert_called_once_with(host,required_mb=1792)
    assert client.call.call_count==2
    reclaim.reset_mock();client.call.side_effect=[{'available_mb':2200}]
    cache.capacity_for_work(host,client,'compute',1792)
    reclaim.assert_not_called()


def test_preparation_protection_is_scoped_and_modern_claims_are_explicit(tmp_path):
    import sqlite3
    path=tmp_path/'queue.sqlite'
    def binding(k):return {'key':k,'container':'swarm-job-'+k[:32],'volume':'swarm-cache-'+k[:32]}
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE submissions(state TEXT,host TEXT,request TEXT)')
        db.execute('INSERT INTO submissions VALUES(?,?,?)',('completed','mini',json.dumps({'cache_inputs':[binding('a'*64),binding('b'*64)]})))
        db.execute('INSERT INTO submissions VALUES(?,?,?)',('accepted','other',json.dumps({'inputs_ready':False})))
        db.execute('INSERT INTO submissions VALUES(?,?,?)',('queued',None,json.dumps({'inputs_ready':False})))
    assert cache.finished_cache_keys({'host':'mini'},path)=={'a'*64,'b'*64}
    with sqlite3.connect(path) as db:
        db.execute('INSERT INTO submissions VALUES(?,?,?)',('accepted','mini',json.dumps({'inputs_ready':False})))
    assert cache.finished_cache_keys({'host':'mini'},path)==set()
    with sqlite3.connect(path) as db:
        db.execute("UPDATE submissions SET request=? WHERE state='accepted' AND host='mini'",(json.dumps({'inputs_ready':False,'cache_claims_version':1,'cache_inputs':[binding('b'*64)]}),))
    assert cache.finished_cache_keys({'host':'mini'},path)=={'a'*64}


def test_binding_checkpoint_failure_prevents_cache_lookup_and_creation(monkeypatch):
    seen=[]
    def checkpoint(binding):
        seen.append(binding)
        raise RuntimeError('queue unavailable')
    monkeypatch.setattr(cache,'inspect',lambda *a:pytest.fail('Cache lookup preceded durable queue ownership'))
    files={'index':{'bytes':4,'sha256':hashlib.sha256(b'data').hexdigest()}}
    with pytest.raises(RuntimeError,match='queue unavailable'):
        cache.ensure({'host':'mini'},IMAGE,files,lambda _:pytest.fail('Loaded unowned input'),on_bound=checkpoint)
    assert seen[0]['host']=='mini'
    assert seen[0]['key']==cache.contract(IMAGE,files)[0]


def ownership_queue(tmp_path, monkeypatch):
    import sqlite3
    path=tmp_path/'ownership.sqlite'
    monkeypatch.setenv('SWARM_SUBMISSION_QUEUE',str(path))
    db=sqlite3.connect(path)
    db.execute('PRAGMA journal_mode=WAL')
    db.execute('CREATE TABLE submissions(state TEXT,host TEXT,request TEXT)')
    db.commit()
    return path,db


def ownership_binding(key):
    return {'key':key,'container':'swarm-job-'+key[:32],'volume':'swarm-cache-'+key[:32]}


def test_ownership_reuses_unchanged_rows_but_observes_wal_claims(tmp_path,monkeypatch):
    path,db=ownership_queue(tmp_path,monkeypatch)
    a,b='a'*64,'b'*64
    for host,key in [('mini',a),('other',b)]:
        db.execute('INSERT INTO submissions VALUES(?,?,?)',('completed',host,json.dumps({'cache_inputs':[ownership_binding(key)]})))
    db.commit()
    parse=cache._QueueOwnership.parse;calls=[]
    def measured(request,node):calls.append(node);return parse(request,node)
    monkeypatch.setattr(cache._QueueOwnership,'parse',staticmethod(measured))
    try:
        assert cache.finished_cache_keys({'host':'mini'},path)=={a}
        assert cache.finished_cache_keys({'host':'other'},path)=={b}
        assert len(calls)==2  # Both nodes share one parsed queue snapshot.
        returned=cache.finished_cache_keys({'host':'mini'},path);returned.clear()
        assert cache.finished_cache_keys({'host':'mini'},path)=={a}
        db.execute('INSERT INTO submissions VALUES(?,?,?)',('accepted','mini#gpu',json.dumps({'cache_claims_version':1,'inputs_ready':False,'cache_inputs':[ownership_binding(a)]})))
        db.commit()
        assert cache.finished_cache_keys({'host':'mini'},path)==set()
        assert len(calls)==3  # Existing historical requests were not reparsed.
        db.execute("UPDATE submissions SET state='completed' WHERE state='accepted'");db.commit()
        assert cache.finished_cache_keys({'host':'mini'},path)=={a}
        assert len(calls)==3  # A state change uses existing parsed bindings.
    finally:db.close()


def test_ownership_refreshes_request_edits_deletes_and_database_replacement(tmp_path,monkeypatch):
    import sqlite3
    path,db=ownership_queue(tmp_path,monkeypatch)
    a,b='a'*64,'b'*64
    request=lambda key:json.dumps({'cache_inputs':[ownership_binding(key)]})
    try:
        db.execute('INSERT INTO submissions VALUES(?,?,?)',('completed','mini',request(a)));db.commit()
        assert cache.finished_cache_keys({'host':'mini'},path)=={a}
        db.execute('UPDATE submissions SET request=?',(request(b),));db.commit()
        assert cache.finished_cache_keys({'host':'mini'},path)=={b}
        db.execute('DELETE FROM submissions');db.commit()
        assert cache.finished_cache_keys({'host':'mini'},path)==set()
    finally:
        # A valid DB replacement must not replay the previous file's WAL.
        db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        db.close()
    replacement=tmp_path/'replacement.sqlite'
    with sqlite3.connect(replacement) as other:
        other.execute('CREATE TABLE submissions(state TEXT,host TEXT,request TEXT)')
        other.execute('INSERT INTO submissions VALUES(?,?,?)',('completed','mini',request(a)))
    replacement.replace(path)
    assert cache.finished_cache_keys({'host':'mini'},path)=={a}
    path.unlink()
    assert cache.finished_cache_keys({'host':'mini'},path)==set()


def test_ownership_invalid_request_never_reuses_previous_eligibility(tmp_path,monkeypatch):
    path,db=ownership_queue(tmp_path,monkeypatch)
    key='a'*64
    try:
        db.execute('INSERT INTO submissions VALUES(?,?,?)',('completed','mini',json.dumps({'cache_inputs':[ownership_binding(key)]})));db.commit()
        assert cache.finished_cache_keys({'host':'mini'},path)=={key}
        db.execute("INSERT INTO submissions VALUES('accepted','other','{corrupt')");db.commit()
        assert cache.finished_cache_keys({'host':'mini'},path)==set()
        db.execute("DELETE FROM submissions WHERE state='accepted'");db.commit()
        assert cache.finished_cache_keys({'host':'mini'},path)=={key}
    finally:db.close()


@pytest.fixture
def reclaim_inventory(tmp_path,monkeypatch):
    from unittest.mock import Mock
    path,db=ownership_queue(tmp_path,monkeypatch)
    keys=['a'*64,'b'*64,'c'*64]
    # The oldest cache is also the heavily reused one; preserve it on pressure.
    rows=[{'Config':{'Labels':{cache.OWNER:key}},'Name':'/swarm-job-'+key[:32],
           'Created':str(index),'HostConfig':{'Memory':1024**3}} for index,key in enumerate(reversed(keys))]
    for key in keys:
        for _ in range(5 if key==keys[2] else 1):
            db.execute('INSERT INTO submissions VALUES(?,?,?)',('completed','mini',json.dumps({'cache_inputs':[ownership_binding(key)]})))
    db.commit()
    removed=[];available=[700]
    def command(host,args,**kwargs):
        if args[:3]==['docker','ps','-aq']:return 'one two three'
        if args[:2]==['docker','inspect']:return json.dumps(rows)
        pytest.fail('Unexpected Docker mutation')
    def inspect(host,kind,name):
        assert kind=='volume'
        key=next(key for key in keys if name=='swarm-cache-'+key[:32])
        return {'Driver':'local','Options':{'type':'tmpfs'},'Labels':{cache.OWNER:key}}
    def remove(host,binding):
        removed.append(binding['key']);available[0]+=175;return True
    client=Mock();client.call.side_effect=lambda *args,**kw:{'available_mb':available[0]}
    monkeypatch.setattr(cache,'command',command);monkeypatch.setattr(cache,'inspect',inspect)
    monkeypatch.setattr(cache,'_remove_unreferenced',remove)
    monkeypatch.setattr(cache,'AgentClient',Mock(return_value=client))
    try:yield keys,removed,available,client
    finally:db.close()


def test_pressure_stops_at_observed_headroom_and_preserves_high_reuse(reclaim_inventory):
    keys,removed,available,client=reclaim_inventory
    assert cache.reclaim_completed({'host':'mini'},required_mb=1000)==2
    assert set(removed)==set(keys[:2])
    assert available[0]==1050
    assert client.call.call_count==3  # Initial read, then each successful removal.
    client.close.assert_called_once()


def test_pressure_already_satisfied_does_not_enumerate_or_evict(reclaim_inventory,monkeypatch):
    keys,removed,available,client=reclaim_inventory
    available[0]=1500
    monkeypatch.setattr(cache,'command',lambda *a,**k:pytest.fail('Enough headroom already'))
    assert cache.reclaim_completed({'host':'mini'},required_mb=1000)==0
    assert removed==[]
    client.close.assert_called_once()


def test_background_sweep_remains_aggressive(reclaim_inventory):
    keys,removed,available,client=reclaim_inventory
    assert cache.reclaim_completed({'host':'mini'})==3
    assert set(removed)==set(keys)
    client.call.assert_not_called()


def test_new_claim_during_docker_inspection_prevents_eviction(reclaim_inventory,monkeypatch):
    keys,removed,available,client=reclaim_inventory
    original=cache.inspect
    def claim(host,kind,name):
        import sqlite3
        row=original(host,kind,name)
        key=next(key for key in keys if name=='swarm-cache-'+key[:32])
        with sqlite3.connect(__import__('os').environ['SWARM_SUBMISSION_QUEUE']) as db:
            db.execute('INSERT INTO submissions VALUES(?,?,?)',('accepted','mini',json.dumps({'cache_claims_version':1,'inputs_ready':False,'cache_inputs':[ownership_binding(key)]})))
        return row
    monkeypatch.setattr(cache,'inspect',claim)
    assert cache.reclaim_completed({'host':'mini'},required_mb=1000)==0
    assert removed==[]


def test_unknown_headroom_fails_closed_before_eviction(reclaim_inventory,monkeypatch):
    keys,removed,available,client=reclaim_inventory
    client.call.side_effect=None;client.call.return_value={'available_mb':None}
    with pytest.raises(ValueError,match='headroom'):
        cache.reclaim_completed({'host':'mini'},required_mb=1000)
    assert removed==[]
    client.close.assert_called_once()


def test_placement_observes_concurrently_freed_headroom_without_own_eviction(monkeypatch):
    from unittest.mock import Mock
    client=Mock();client.call.side_effect=[{'available_mb':512},{'available_mb':2200}]
    reclaim=Mock(return_value=0);monkeypatch.setattr(cache,'reclaim_completed',reclaim)
    assert cache.capacity_for_work({'host':'mini'},client,'compute',1792)['available_mb']==2200
    reclaim.assert_called_once_with({'host':'mini'},required_mb=1792)
