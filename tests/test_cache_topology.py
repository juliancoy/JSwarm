from concurrent.futures import Future
import hashlib
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from scripts.swarm_cache_topology import CacheTopology
from scripts.swarm_shared_cache import contract


def entry(data=b'contig', image='sha256:'+'a'*64):
    files={'reference.fa':{'bytes':len(data),'sha256':hashlib.sha256(data).hexdigest()}}
    key,value=contract(image,files)
    return {**value,'key':key,'container':'swarm-job-'+key[:32]}


@pytest.fixture
def topology():
    now=[100]
    hosts={n:{'host':n,'machine':n} for n in ('cold','warm','other')}
    t=CacheTopology(hosts,clock=lambda:now[0]);t.now=now
    yield t
    t.pool.shutdown(wait=True)


def test_affinity_requires_fresh_complete_immutable_object_set(topology):
    c=entry();topology.observations['warm']=(100,[c])
    assert topology.local_bytes({'host':'warm'},[c['files']])==6
    changed=entry(b'changed')
    assert topology.local_bytes({'host':'warm'},[changed['files']])==0
    topology.now[0]=281
    assert topology.local_bytes({'host':'warm'},[c['files']])==0


def test_inventory_rejects_changed_machine_before_reading_cache(topology):
    client=Mock();client.call.return_value={'node':'warm','machine':'replacement'}
    topology.client=lambda h:client
    with pytest.raises(ValueError,match='identity changed'):topology._inventory(topology.hosts['warm'])
    client.call.assert_called_once_with('status',timeout=10);client.close.assert_called_once()


def test_background_refresh_does_not_wait_for_offline_inventory(topology):
    topology.pool.shutdown(wait=True)
    pending=Future();topology.pool=Mock();topology.pool.submit.return_value=pending
    topology.refresh();topology.refresh()
    assert topology.pool.submit.call_count==3
    assert not topology.entries('warm')
    pending.set_result((100,[entry()]))
    topology.refresh()
    assert topology.entries('warm')


def test_peer_selection_uses_success_rate_load_and_failure_cooldown(topology):
    cache=entry()
    for name in ('warm','other'):topology.observations[name]=(100,[cache])
    dest={'host':'cold'}
    topology.transfer_finished({'host':'warm'},dest,100,1,success=True)
    topology.transfer_finished({'host':'other'},dest,100,10,success=True)
    assert topology.peers(dest,cache['files'])[0][0]['host']=='warm'
    topology.transfer_started({'host':'warm'},dest)
    assert topology.peers(dest,cache['files'])[0][0]['host']=='other'
    topology.transfer_finished({'host':'warm'},dest,100,1,success=False)
    assert [h['host'] for h,c in topology.peers(dest,cache['files'])]==['other']
    topology.now[0]+=61
    assert len(topology.peers(dest,cache['files']))==2


def test_derivation_hint_reuses_existing_identity_metadata(tmp_path):
    cache=entry();identity={'source_sha256':'b'*64,'contig':'1'}
    key=hashlib.sha256(json.dumps({'identity':identity,'image':cache['image']},sort_keys=True,separators=(',',':')).encode()).hexdigest()
    path=tmp_path/(key+'.json');path.write_text(json.dumps({'identity':identity,'cache':cache}))
    assert CacheTopology.derived_files(identity,cache['image'],directory=tmp_path)==cache['files']
    path.write_text(json.dumps({'identity':{**identity,'contig':'2'},'cache':cache}))
    assert CacheTopology.derived_files(identity,cache['image'],directory=tmp_path) is None


def test_queue_affinity_preserves_priority_capacity_and_attempt_ownership(tmp_path,monkeypatch):
    from submission_queue import SubmissionQueue
    monkeypatch.setenv('SWARM_SUBMISSION_QUEUE',str(tmp_path/'queue.sqlite'))
    q=SubmissionQueue('locality')
    q.submit_many([{'id':'urgent','priority':100,'resources':{'memory_mb':256}},
                   {'id':'warm','resources':{'memory_mb':256}},
                   {'id':'cold','resources':{'memory_mb':256}},
                   {'id':'too-large','resources':{'memory_mb':4096}}])
    capacity={'available_mb':512,'available_cpus':4};affinity={'too-large':10000,'warm':1000}
    first=q.claim('node',capacity,affinity=affinity)
    assert first['id']=='urgent'
    second=q.claim('node',capacity,affinity=affinity)
    assert second['id']=='warm' and second['token']!=first['token']
    # Already claimed work is never migrated by a later locality observation.
    assert q.claim('other',capacity,affinity={'urgent':100000})['id']=='cold'


def test_transfer_rejects_changed_objects_before_opening_any_process(monkeypatch):
    import scripts.swarm_cache_transfer as transfer
    cache=entry();other=entry(b'bad')
    monkeypatch.setattr(transfer,'shell_command',Mock(side_effect=AssertionError('Must not run a command')))
    import tempfile,pathlib
    with tempfile.TemporaryDirectory() as directory:
        config=pathlib.Path(directory)/'peers.json';config.write_text(json.dumps({'peers':{'a':'10.42.0.1','b':'10.42.0.2'}}))
        import scripts.swarm_runtime as runtime
        monkeypatch.setattr(runtime,'CONFIG',config)
        with pytest.raises(ValueError,match='objects differ'):
            transfer.copy({'host':'a'},cache,{'host':'b'},other,'receive')
