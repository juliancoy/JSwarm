"""Existing job records protect shared inputs; no second ownership ledger."""
import contextlib
import hashlib
import io
import json
import sqlite3

import pytest
from scripts import launcher_streams as streams
from scripts import swarm_shared_cache as caches


def binding(key='a'*64,host='node'):
    return {'key':key,'host':host,'container':'swarm-job-'+key[:32],'volume':'swarm-cache-'+key[:32]}


@pytest.fixture
def queue(tmp_path,monkeypatch):
    path=tmp_path/'queue.sqlite';monkeypatch.setenv('SWARM_SUBMISSION_QUEUE',str(path))
    with sqlite3.connect(path) as db:db.execute('CREATE TABLE submissions(id TEXT,state TEXT,host TEXT,request TEXT)')
    def put(ident,state,request,host='node'):
        with sqlite3.connect(path) as db:
            db.execute('DELETE FROM submissions WHERE id=?',(ident,))
            db.execute('INSERT INTO submissions VALUES(?,?,?,?)',(ident,state,host,json.dumps(request)))
    return put,path


def test_prepared_jobs_keep_ram_until_last_confirmed_terminal_event(queue,monkeypatch):
    put,_=queue;cache=binding();host={'machine':'fixture','host':'node'}
    alive={'container':True,'volume':True};consumers=[];deleted=[]
    monkeypatch.setattr(caches,'users',lambda *_:list(consumers))
    monkeypatch.setattr(caches,'inspect',lambda host,kind,name:{'Name':name,'Id':'owner-id'} if alive[kind] else None)
    def command(host,argv,**kwargs):
        kind='container' if argv[:3]==['docker','rm','-f'] else 'volume'
        alive[kind]=False;deleted.append(kind);return ''
    monkeypatch.setattr(caches,'command',command)
    request={'transport':{'cache':cache}}
    put('job-a','completed',request);put('job-b','accepted',request)
    assert caches.evict_unused(host,keys={cache['key']},required_mb=1)==0
    assert not caches.remove_unused(host,cache,terminal='completed')
    put('job-b','retry_wait',request)
    assert not caches.remove_unused(host,cache,terminal='completed')
    put('job-b','stopping',request)
    assert not caches.remove_unused(host,cache,terminal='cancelled')
    put('job-b','cancelled',request);consumers.append('worker-b')
    assert not caches.remove_unused(host,cache,terminal='cancelled')
    assert not deleted
    consumers.clear()
    assert caches.remove_unused(host,cache,terminal='cancelled')
    assert deleted==['container','volume']


def test_source_cache_and_cpu_witness_use_the_same_job_records(queue):
    put,path=queue;cache=binding(host='source')
    put('done','completed',{'cache_inputs':[cache]},host='node')
    put('witness','queued',{'transport':{'source_caches':[cache]}},host='other#cpu')
    assert caches.finished_cache_keys({'host':'source'},path)==set()
    put('witness','completed',{'transport':{'source_caches':[cache]}},host='other#cpu')
    assert caches.finished_cache_keys({'host':'source'},path)=={cache['key']}
    assert caches.finished_cache_keys({'host':'node'},path)==set()


def test_partial_preparation_unknown_or_missing_queue_never_authorizes_deletion(queue):
    put,path=queue;cache=binding();host={'host':'node'}
    put('done','completed',{'transport':{'cache':cache}})
    put('preparing','accepted',{'inputs_ready':False})
    assert caches.finished_cache_keys(host)==set()
    put('preparing','completed',{})
    assert caches.finished_cache_keys(host)=={cache['key']}
    with sqlite3.connect(path) as db:db.execute("UPDATE submissions SET request='broken' WHERE id='preparing'")
    assert caches.finished_cache_keys(host)==set()
    assert caches.finished_cache_keys(host,path.parent/'missing')==set()


def test_closed_input_reader_does_not_make_job_cache_evictable(tmp_path):
    data=b'first';row={'sha256':hashlib.sha256(data).hexdigest(),'bytes':len(data)}
    reader=streams.shared_input(row,lambda:io.BytesIO(data),tmp_path,len(data));reader.close()
    other=b'next!';request={'sha256':hashlib.sha256(other).hexdigest(),'bytes':len(other)}
    fallback=streams.shared_input(request,lambda:io.BytesIO(other),tmp_path,len(data))
    assert isinstance(fallback,io.BytesIO)
    assert (tmp_path/row['sha256']).read_bytes()==data
    assert not (tmp_path/request['sha256']).exists()


def test_losing_loader_does_not_remove_or_release_winning_loader(monkeypatch):
    from unittest.mock import Mock
    client=Mock();client.call.return_value={'state':'waiting'}
    monkeypatch.setattr(caches,'AgentClient',lambda _:client)
    load=Mock();remove=Mock();monkeypatch.setattr(caches,'_remove_unreferenced',remove)
    with pytest.raises(caches.CapacityWait):
        caches.ensure({'host':'node','machine':'machine'},'sha256:'+'b'*64,{'input':{'bytes':1,'sha256':'c'*64}},load)
    load.assert_not_called();remove.assert_not_called()
    assert [call.args[0] for call in client.call.call_args_list]==['resource.cache.begin']


def test_verified_cache_survives_failure_after_publication(monkeypatch):
    from unittest.mock import Mock
    client=Mock();client.call.side_effect=[{'state':'ready'},RuntimeError('Connection lost after seal')]
    monkeypatch.setattr(caches,'AgentClient',lambda _:client)
    remove=Mock();monkeypatch.setattr(caches,'_remove_unreferenced',remove)
    with pytest.raises(caches.CapacityWait):
        caches.ensure({'host':'node','machine':'machine'},'sha256:'+'b'*64,{'input':{'bytes':1,'sha256':'c'*64}},lambda *_:pytest.fail('Reloaded sealed node object'))
    remove.assert_not_called()
    assert [call.args[0] for call in client.call.call_args_list]==['resource.cache.begin','resource.cache.check']
