import hashlib
import json
import subprocess

import pytest
from scripts import swarm_shared_cache as cache

IMAGE='sha256:2817b8f8ff61d06d2e260007d79a51c676e22eb4e7b619a9c336422db039412e'


def test_contract_uses_content_not_source_path_and_rejects_unsafe_names():
    row={'bytes':4,'sha256':hashlib.sha256(b'data').hexdigest(),'path':'/one'}
    assert cache.contract(IMAGE,{'index':row})==cache.contract(IMAGE,{'index':{**row,'path':'/two'}})
    with pytest.raises(ValueError):cache.contract(IMAGE,{'../index':row})
    with pytest.raises(ValueError):cache.contract('latest',{'index':row})


def test_slow_daemon_cleanup_is_deferred(monkeypatch):
    monkeypatch.setattr(cache,'users',lambda *args:[])
    monkeypatch.setattr(cache,'inspect',lambda *args:{'Id':'owned-cache'})
    def slow(*args,**kwargs):raise subprocess.TimeoutExpired('docker rm',45)
    monkeypatch.setattr(cache,'command',slow)
    assert cache._remove_unreferenced({}, {'container':'cache','volume':'cache-volume'}) is False


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
