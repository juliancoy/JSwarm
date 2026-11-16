import base64
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import mmap
import os
from pathlib import Path
import subprocess
import sys
import time
import socket
import threading
import errno
from unittest.mock import Mock

import pytest
from scripts.swarm_memory import PageReader, seal, validate, resolve_mount
from scripts.run_input_cache import InputCache
from scripts.swarm_memory_mount import ObjectView


def fixture(tmp_path, *, budget=1024**2):
    data = bytes(range(251))*1100
    source=tmp_path/'original';source.write_bytes(data);source.chmod(0o400)
    manifest=seal(source,hashlib.sha256(data).hexdigest(),4096)
    cache=InputCache(tmp_path/'cache',budget=budget,reserve=0)
    return data,manifest,cache


def test_manifest_requires_expected_digest_and_immutable_regular_source(tmp_path):
    data,manifest,_=fixture(tmp_path)
    assert validate(json.loads(json.dumps(manifest)))==manifest
    manifest['pages'][0]='a'*64
    with pytest.raises(ValueError):validate(manifest)
    with pytest.raises(ValueError):seal(tmp_path/'original','0'*64)
    (tmp_path/'original').chmod(0o600)
    with pytest.raises(ValueError):seal(tmp_path/'original',hashlib.sha256(data).hexdigest())


def test_mount_resolution_requires_content_machine_and_verification():
    ident='a'*64;rev='b'*64
    row=dict(ready=True,machine='machine',object_id=ident,revision=rev,path=f'/home/owner/SwarmMemory/{ident}/{rev}/object')
    deployment=dict(object_id=ident,nodes={'node.local':row})
    assert resolve_mount(deployment,ident,'node.local','machine')==row
    assert resolve_mount(deployment,ident,'node.local','other') is None
    row['ready']=False
    assert resolve_mount(deployment,ident,'node.local','machine') is None
    row.update(ready=True,path='/etc/passwd')
    assert resolve_mount(deployment,ident,'node.local','machine') is None


def test_arbitrary_ranges_eof_eviction_and_concurrent_fault_coalescing(tmp_path):
    data,manifest,cache=fixture(tmp_path,budget=8300)
    fetch=Mock(side_effect=lambda start,length:data[start:start+length])
    reader=PageReader(manifest,cache,fetch,Mock(),namespace='test')
    with ThreadPoolExecutor(max_workers=8) as pool:
        assert list(pool.map(lambda _:reader.read(5,4000),range(8)))==[data[5:4005]]*8
    assert fetch.call_count==1
    assert reader.read(8189,5000)==data[8189:13189]
    assert reader.read(len(data)-13,999)==data[-13:]
    assert reader.read(len(data),9)==b''
    assert sum(p.stat().st_size for p in cache.root.glob('*.block'))<=cache.budget
    assert reader.read(0,17)==data[:17]  # Refetch an evicted page.


def test_shared_content_cache_survives_origin_outage_and_rejects_poison(tmp_path):
    data,manifest,cache=fixture(tmp_path)
    peers={};calls=[]
    def call(peer,op,**args):
        calls.append((peer,op))
        key=(peer,args['key'])
        if op=='cache.put':peers[key]=args['data'];return {'stored':True}
        return {'data':peers.get(key)}
    first=PageReader(manifest,cache,lambda start,length:data[start:start+length],call,
                     ['one.local','two.local'],namespace='test')
    assert first.read(0,4096)==data[:4096]
    first.pending.join();first.close()
    assert len(peers)==2
    unavailable=Mock(side_effect=OSError('origin unavailable'))
    second=PageReader(manifest,InputCache(tmp_path/'other',reserve=0),unavailable,call,
                      ['one.local','two.local'],namespace='test')
    assert second.read(0,128)==data[:128];unavailable.assert_not_called()
    second.pending.join();second.close()
    # A malicious/self-consistent cache entry must not pass publisher hashes.
    corrupt=b'x'*4096
    poisoned=InputCache(tmp_path/'poison',reserve=0)
    key=first.key(0);poisoned.block(key,4096,0,lambda *_:corrupt)
    for p in peers:peers[p]=base64.b64encode(corrupt).decode()
    repaired=PageReader(manifest,poisoned,lambda start,length:data[start:start+length],call,
                        ['one.local','two.local'],namespace='test',replicas=0)
    assert repaired.read(0,128)==data[:128]
    assert repaired.stats['corrupt_local_pages']==1
    assert repaired.stats['corrupt_peer_pages']==2
    assert poisoned.lookup(key,4096,0)==data[:4096]


def test_corrupt_source_and_wrong_namespace_do_not_satisfy_object(tmp_path):
    data,manifest,cache=fixture(tmp_path)
    reader=PageReader(manifest,cache,lambda *_:b'x'*4096,Mock(return_value={'data':None}),namespace='private')
    with pytest.raises(OSError):reader.read(0,1)
    other=PageReader(manifest,cache,Mock(),Mock(),namespace='other')
    assert reader.key(0)!=other.key(0)
    assert not list(cache.root.glob('*.block'))


def test_blackholed_first_peer_leaves_time_for_alternate(tmp_path):
    data,manifest,cache=fixture(tmp_path);now=[0.0];attempts=[]
    def call(peer,op,**args):
        attempts.append(args['timeout'])
        if len(attempts)==1:
            now[0]+=args['timeout'];raise OSError('Blackholed route')
        return {'data':base64.b64encode(data[:4096]).decode()}
    fetch=Mock(side_effect=OSError('Origin down'))
    reader=PageReader(manifest,cache,fetch,call,['a.local','b.local'],namespace='test',replicas=0,clock=lambda:now[0])
    assert reader.read(0,32)==data[:32]
    assert len(attempts)==2 and sum(attempts)<=.5
    fetch.assert_not_called()


def test_secondary_authoritative_source_and_reconnection(tmp_path):
    data,manifest,cache=fixture(tmp_path)
    definition=dict(source='a.local',machine='a'*64,sha256=manifest['sha256'],bytes=len(data),clients={})
    transport=Mock(stats={})
    transport.read.side_effect=[OSError('path lost'),OSError('origin down'),data[:4096]]
    view=ObjectView(dict(object=manifest,sources=[definition,{**definition,'source':'b.local'}],filesystem={'namespace':'private','peers':[]}),
                    cache=cache,peer_client=Mock(),transport=transport)
    assert view.pages.read(0,12)==data[:12]
    assert [c.args[0] for c in transport.read.call_args_list]==['0','0','1']
    assert view.pages.stats['source_transport_errors']==2
    assert json.loads(view.metrics())['last_source_error']=='origin down'
    view.close()


def test_transport_connection_state_survives_ephemeral_callback_threads(tmp_path):
    data,manifest,cache=fixture(tmp_path)
    local=threading.local();opened=[]
    def read(path,offset,length):
        if not hasattr(local,'connected'):
            local.connected=True;opened.append(threading.get_ident())
        return data[offset:offset+length]
    transport=Mock(stats={});transport.read.side_effect=read
    source=dict(source='a.local',sha256=manifest['sha256'],bytes=len(data))
    view=ObjectView(dict(object=manifest,sources=[source],filesystem={'namespace':'test','peers':[]}),
                    cache=cache,peer_client=Mock(),transport=transport)
    results=[]
    try:
        for i in range(8):
            callback=threading.Thread(target=lambda i=i:results.append(view.pages.read(i*4096,16)))
            callback.start();callback.join(timeout=5);assert not callback.is_alive()
        assert results==[data[i*4096:i*4096+16] for i in range(8)]
        assert len(opened)==1
    finally:view.close()


def test_dual_family_connection_fallback_and_refresh(monkeypatch):
    from scripts import swarm_immutable_read as module
    connect=module.connect
    monkeypatch.setattr(module,'service_candidates',lambda *a:[])
    with socket.socket(socket.AF_INET,socket.SOCK_STREAM) as v4, socket.socket(socket.AF_INET6,socket.SOCK_STREAM) as v6:
        v4.bind(('127.0.0.1',0));v4.listen()
        v6.setsockopt(socket.IPPROTO_IPV6,socket.IPV6_V6ONLY,1)
        v6.bind(('::1',0));v6.listen()
        addresses=[(socket.AF_INET6,socket.SOCK_STREAM,6,'',('::1',v4.getsockname()[1],0,0)),
                   (socket.AF_INET,socket.SOCK_STREAM,6,'',v4.getsockname())]
        monkeypatch.setattr(socket,'getaddrinfo',lambda *a:addresses)
        with connect('enrolled.local',1,timeout=.2) as connection:
            assert connection.family==socket.AF_INET
        # The next connection resolves again and succeeds after IPv4 disappears.
        addresses=[(socket.AF_INET6,socket.SOCK_STREAM,6,'',v6.getsockname())]
        with connect('enrolled.local',1,timeout=.2) as connection:
            assert connection.family==socket.AF_INET6


def test_scoped_ipv6_and_ordinary_tcp_fallback(monkeypatch):
    from scripts import swarm_immutable_read as module
    monkeypatch.setattr(module,'service_candidates',lambda *a:[])
    endpoint=('fe80::1234',9448,0,7);opened=[];connected=[]
    monkeypatch.setattr(socket,'getaddrinfo',lambda *a:[(socket.AF_INET6,socket.SOCK_STREAM,6,'',endpoint)])
    class Sock:
        def settimeout(self,*a):pass
        def setsockopt(self,*a):pass
        def connect(self,addr):connected.append(addr)
        def close(self):pass
    def create(family,kind,protocol):
        opened.append(protocol)
        if protocol==getattr(socket,'IPPROTO_MPTCP',262):raise OSError(errno.EPROTONOSUPPORT,'fixture')
        return Sock()
    monkeypatch.setattr(socket,'socket',create)
    module.connect('enrolled.local',9448).close()
    assert connected==[endpoint] and opened[-1]==socket.IPPROTO_TCP


def test_dns_sd_can_connect_while_host_resolution_is_stalled(monkeypatch):
    from scripts import swarm_immutable_read as module
    release=threading.Event()
    def stalled(*args):release.wait(5);return []
    with socket.socket(socket.AF_INET6,socket.SOCK_STREAM) as server:
        server.bind(('::1',0));server.listen()
        monkeypatch.setattr(socket,'getaddrinfo',stalled)
        monkeypatch.setattr(module,'service_candidates',lambda *a:[(socket.AF_INET6,socket.SOCK_STREAM,6,'',server.getsockname())])
        try:
            with module.connect('enrolled.local',9448,timeout=.2) as connection:
                assert connection.family==socket.AF_INET6
                assert not release.is_set()
        finally:release.set()


def test_kernel_mmap_in_separate_object_mount(tmp_path):
    import ctypes.util
    deps=Path.home()/'.local/share/swarm-filesystem/dependencies'
    if not Path('/dev/fuse').exists() or not ctypes.util.find_library('fuse3') or not (deps/'mfusepy.py').exists():
        pytest.skip('Host FUSE3 prerequisites unavailable')
    data,manifest,cache=fixture(tmp_path)
    config={'object':manifest,'sources':[dict(source='fixture.local',sha256=manifest['sha256'],bytes=len(data))],
            'filesystem':{'namespace':'fixture','peers':[],'fuse_version':'3','allow_root':False}}
    settings=tmp_path/'config.json';settings.write_text(json.dumps(config))
    mountpoint=tmp_path/'mount';mountpoint.mkdir()
    child='''import json,sys
from pathlib import Path
sys.path[:0]=sys.argv[1:3]
from scripts import swarm_memory_mount as mod
from scripts.run_input_cache import InputCache
class Transport:
 def read(self,path,start,length):
  with open(sys.argv[4],'rb') as f:f.seek(start);return f.read(length)
original=mod.ObjectView
mod.ObjectView=lambda cfg:original(cfg,cache=InputCache(sys.argv[5],budget=16384,reserve=0),peer_client=object(),transport=Transport())
# Provide the peer callback without any peer or origin RPC in this fixture.
class Peer:
 def call(self,*a,**kw):raise AssertionError('unexpected peer RPC')
mod.ObjectView=lambda cfg:original(cfg,cache=InputCache(sys.argv[5],budget=16384,reserve=0),peer_client=Peer(),transport=Transport())
mod.mount(json.loads(Path(sys.argv[3]).read_text()),Path(sys.argv[6]))
'''
    with (tmp_path/'fuse.log').open('w+') as log:
        process=subprocess.Popen([sys.executable,'-c',child,str(deps),str(Path(__file__).resolve().parents[1]),str(settings),str(tmp_path/'original'),str(cache.root),str(mountpoint)],stdout=log,stderr=log)
        try:
            deadline=time.monotonic()+10
            while not os.path.ismount(mountpoint) and process.poll() is None and time.monotonic()<deadline:time.sleep(.05)
            log.seek(0);assert os.path.ismount(mountpoint),log.read()
            with (mountpoint/'object').open('rb') as f, mmap.mmap(f.fileno(),0,access=mmap.ACCESS_READ) as m:
                assert m[4093:17001]==data[4093:17001]
                assert m[-400:]==data[-400:]
            with pytest.raises(OSError):(mountpoint/'object').open('wb')
            stats=json.loads((mountpoint/'metrics.json').read_text())
            assert stats['object']==manifest['id'] and stats['counters']['source_reads']>0
        finally:
            if os.path.ismount(mountpoint):subprocess.run(['fusermount3','-u',str(mountpoint)],capture_output=True,timeout=10)
            if process.poll() is None:process.terminate()
            process.wait(timeout=10)
