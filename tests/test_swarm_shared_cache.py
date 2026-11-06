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
    original_command=cache.command
    def isolated_command(host,args,**kwargs):
        # Real Docker checks must never enumerate production cache owners.
        if args[:3]==['docker','ps','-aq']:
            args=[*args,'--filter','ancestor='+IMAGE]
        return original_command(host,args,**kwargs)
    monkeypatch.setattr(cache,'command',isolated_command)
    return calls


def docker(*args,**kwargs):
    return subprocess.run(['docker',*args],capture_output=True,**kwargs)


@pytest.fixture
def tool_image():
    if docker('image','inspect',IMAGE).returncode:pytest.skip('Approved BWA image unavailable locally')
    return IMAGE


def test_real_concurrent_bwa_shards_share_one_readonly_inode_and_load_once(tmp_path,admitted,tool_image):
    rng=random.Random(137);sequence=''.join(rng.choices('ACGT',k=20000))
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
        with ThreadPoolExecutor(max_workers=2) as pool:
            a,b=list(pool.map(lambda _:cache.ensure(host,tool_image,files,load),range(2)))
        binding=a
        assert a==b and len(loads)==1
        assert cache.inspect(host,'container',binding['container'])['HostConfig']['NanoCpus']==100000000
        assert len([r for op,r in admitted if op=='resource.acquire'])==1
        program=r'''
import errno,hashlib,json,os,pathlib,subprocess,time
while not pathlib.Path('/dev/shm/.test-go').exists():time.sleep(.05)
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
            jobs=[pool.submit(shard,i) for i in range(2)];deadline=time.monotonic()+60
            while len(cache.users(host,binding))<2:
                assert time.monotonic()<deadline
                time.sleep(.1)
            assert cache.remove_unused(host,binding) is False
            assert cache.evict_unused(host)==0
            assert docker('exec',binding['container'],'touch','/dev/shm/.test-go').returncode==0
            results=[f.result() for f in jobs]
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
        if binding:cache.remove_unused(host,binding)


def test_corrupt_cache_is_rejected_and_removed(admitted,tool_image):
    host={'host':'fixture','local':True,'machine':uuid.uuid4().hex}
    files={'index':{'bytes':4,'sha256':hashlib.sha256(b'good').hexdigest()}}
    key,_=cache.contract(tool_image,files)
    def load(container):
        assert docker('exec',container,'python3','-c','import pathlib;pathlib.Path("/dev/shm/index").write_bytes(b"evil")').returncode==0
    with pytest.raises(RuntimeError,match='checksum mismatch'):cache.ensure(host,tool_image,files,load)
    assert cache.inspect(host,'container','swarm-job-'+key[:32]) is None
    assert cache.inspect(host,'volume','swarm-cache-'+key[:32]) is None


def test_stopped_ram_cache_is_reloaded_instead_of_restarted_empty(tmp_path,admitted,tool_image):
    import os
    host={'host':'fixture','local':True,'machine':uuid.uuid4().hex,'container_user':str(os.getuid())+':'+str(os.getgid())}
    files={'index':{'bytes':3,'sha256':hashlib.sha256(b'abc').hexdigest()}}
    loads=[];binding=None
    def load(name):
        loads.append(name)
        r=docker('exec','-i',name,'python3','-c','import pathlib,sys;pathlib.Path("/dev/shm/index").write_bytes(sys.stdin.buffer.read())',input=b'abc',timeout=15)
        assert r.returncode==0,r.stderr.decode()
    try:
        binding=cache.ensure(host,tool_image,files,load)
        first=cache.inspect(host,'container',binding['container'])
        assert first['HostConfig']['RestartPolicy']['Name']=='no'
        assert docker('stop','--time','1',binding['container'],timeout=15).returncode==0
        rebuilt=cache.ensure(host,tool_image,files,load)
        second=cache.inspect(host,'container',rebuilt['container'])
        assert second['Id']!=first['Id'] and len(loads)==2
        assert cache.ready(host,rebuilt,second)
    finally:
        if binding:cache.remove_unused(host,binding)


def test_derived_objects_build_once_and_mount_at_multiple_readonly_targets(tmp_path,admitted,tool_image):
    host={'host':'fixture','local':True,'machine':uuid.uuid4().hex,'container_user':'1000:1000'}
    builds=[];binding=None
    def build(root):builds.append(1);(root/'reference.fa').write_bytes(b'abc')
    def load(owner,root,files):
        p=docker('exec','-i',owner,'python3','-c',"import pathlib,sys;pathlib.Path('/dev/shm/reference.fa').write_bytes(sys.stdin.buffer.read())",input=(root/'reference.fa').read_bytes())
        assert p.returncode==0,p.stderr.decode()
    try:
        with ThreadPoolExecutor(2) as pool:
            results=list(pool.map(lambda _:cache.ensure_derived(host,tool_image,{'source':'sha','contig':'1'},build,load),range(2)))
        binding=results[0]['cache'];assert results[0]==results[1] and len(builds)==1
        args=cache.mount_args(host,binding,target='/references/one',primary=False)
        p=docker('run','--rm','--network','none',*args,tool_image,'python3','-c',"import pathlib;assert pathlib.Path('/references/one/reference.fa').read_bytes()==b'abc'")
        assert p.returncode==0,p.stderr.decode()
        assert 'dst=/references/one,readonly' in ' '.join(args)
        with pytest.raises(ValueError):cache.mount_args(host,binding,target='/')
        # A live bulk reader must keep its source cache from eviction.
        subprocess.run(['docker','exec','-d',binding['container'],'python3','-c','import time;time.sleep(3)'],check=True)
        assert cache.remove_unused(host,binding) is False
        time.sleep(3.2)
    finally:
        if binding:cache.remove_unused(host,binding)
