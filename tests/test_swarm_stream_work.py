import pytest
from scripts.swarm_stream_work import FleetQueue,StreamClient,IntegrityFailure,byte_ranges


@pytest.fixture(autouse=True)
def isolated_submission_queue(tmp_path,monkeypatch):
    monkeypatch.setenv('SWARM_SUBMISSION_QUEUE',str(tmp_path/'shared.sqlite'))


def test_ranges_cover_exactly_and_allow_empty():
    assert list(byte_ranges(10,4))==[{'start':0,'length':4},{'start':4,'length':4},{'start':8,'length':2}]
    assert list(byte_ranges(0))==[]


def test_wait_deadline_applies_while_node_is_disconnected_without_cancelling_work(monkeypatch):
    from scripts import swarm_stream_work as module
    clock=[0.0];monkeypatch.setattr(module.time,'monotonic',lambda:clock[0])
    calls=[]
    class Client:
        def call(self,op,args,timeout):
            calls.append((op,args,timeout));clock[0]+=timeout;raise ConnectionResetError()
        def close(self):pass
    with pytest.raises(TimeoutError,match='retains accepted'):
        StreamClient({'machine':'worker'},client=Client()).wait('accepted',timeout=2)
    assert calls==[('stream.status',{'id':'accepted'},2)]


def test_fleet_queue_claim_recovery_and_input_binding(tmp_path):
    path=tmp_path/'queue.sqlite';units=[{'id':'one','input':'sha'},{'id':'two'}];q=FleetQueue(path,units)
    row=q.claim('node');assert row['id']=='one';assert q.claim('other')['id']=='two'
    q.update('one',state='accepted',task_id='remote-one',request='{}')
    q=FleetQueue(path,units);q.recover_preparations()
    assert q.snapshot()=={'accepted':1,'queued':1} and q.recover('node')[0]['task_id']=='remote-one'
    with pytest.raises(ValueError):FleetQueue(path,[{'id':'one','input':'different'}])


def test_lost_submit_response_retries_same_contract():
    class Client:
        def __init__(self):self.calls=[]
        def close(self):pass
        def call(self,op,args):
            self.calls.append((op,args))
            if len(self.calls)==1:raise ConnectionResetError()
            return {'id':args['id'],'state':'queued'}
    transport=Client();c=StreamClient({'machine':'worker'},client=transport)
    assert c.submit({'id':'same'})['id']=='same'
    assert transport.calls[0]==transport.calls[1]


def test_cached_result_must_match_contract():
    class Client:
        def close(self):pass
        def call(self,*args):return {'state':'completed','contract_sha256':'correct','result':{'contract_sha256':'wrong','worker_machine':'worker'}}
    with pytest.raises(IntegrityFailure):StreamClient({'machine':'worker'},client=Client()).run({'id':'same'})


def test_fleet_recovery_returns_to_accepting_launcher_without_repreparation(tmp_path,monkeypatch):
    import scripts.swarm_stream_work as w
    path=tmp_path/'queue.sqlite';units=[{'id':'one'},{'id':'two'}];q=FleetQueue(path,units)
    q.claim('node');q.update('one',state='accepted',task_id='remote-one',request='{"id":"remote-one"}')
    calls=[];prepared=[]
    class Client:
        def __init__(self,host,*args):self.host=host
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def capabilities(self):return True
        def call(self,*args):return {'accepting_jobs':True}
        def run(self,spec,progress=lambda row:None):calls.append((self.host['host'],spec['id']));return {'state':'completed'}
    monkeypatch.setattr(w,'StreamClient',Client)
    def prepare(unit,host):prepared.append(unit['id']);return {'id':'remote-'+unit['id']}
    assert w.distribute(units,[{'host':'node'}],prepare,path)=={'completed':2}
    assert calls==[('node','remote-one'),('node','remote-two')] and prepared==['two']


def test_recovered_jobs_use_all_configured_slots(tmp_path,monkeypatch):
    import scripts.swarm_stream_work as w
    path=tmp_path/'queue.sqlite';units=[{'id':'one'},{'id':'two'}];q=FleetQueue(path,units)
    for _ in units:
        row=q.claim('node');q.update(row['id'],state='accepted',task_id=row['id'],request='{"id":"'+row['id']+'"}')
    clients=[]
    class Client:
        def __init__(self,*args):self.jobs=[];clients.append(self)
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def capabilities(self):return True
        def call(self,*args):return {'accepting_jobs':True}
        def run(self,spec,progress=lambda row:None):self.jobs.append(spec['id']);return {'state':'completed'}
    monkeypatch.setattr(w,'StreamClient',Client)
    assert w.distribute(units,[{'host':'node'}],lambda *a:pytest.fail('Accepted work must not be prepared again'),path,slots={'node':2})=={'completed':2}
    assert sorted(c.jobs for c in clients)==[['one'],['two']]


def test_default_feeder_slots_follow_node_cpu_capacity(tmp_path,monkeypatch):
    import threading
    import scripts.swarm_stream_work as w
    barrier=threading.Barrier(3);active=[]
    class Client:
        def __init__(self,host,*args):pass
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def capabilities(self):return True
        def call(self,*args):return {'accepting_jobs':True}
        def run(self,spec,progress=lambda row:None):
            active.append(spec['id']);barrier.wait(timeout=5)
            return {'state':'completed'}
    monkeypatch.setattr(w,'StreamClient',Client)
    result=w.distribute([{'id':str(i)} for i in range(3)],[{'host':'node','cpus':3}],lambda u,h:u,tmp_path/'queue.sqlite')
    assert result=={'completed':3}
    assert len(active)==3


def test_legacy_journal_migrates_accepted_work_into_shared_queue(tmp_path,monkeypatch):
    import sqlite3
    from scripts.launcher_streams import canonical,digest
    legacy=tmp_path/'legacy.sqlite';shared=tmp_path/'submission.sqlite'
    unit={'id':'one'}
    with sqlite3.connect(legacy) as db:
        db.execute('CREATE TABLE units(id TEXT PRIMARY KEY,fingerprint TEXT,spec TEXT,state TEXT,host TEXT,task_id TEXT,request TEXT,result TEXT,error TEXT)')
        db.execute('INSERT INTO units VALUES(?,?,?,?,?,?,?,?,?)',('one',digest(unit),canonical(unit),'accepted','node','remote-one','{}',None,None))
    monkeypatch.setenv('SWARM_SUBMISSION_QUEUE',str(shared))
    q=FleetQueue(legacy,[unit]);q.recover_preparations()
    assert q.queue.path==shared and q.claim('other') is None
    row=q.recover('node')[0];assert row['task_id']=='remote-one'
    q.update('one',state='completed',result='{}')
    q=FleetQueue(legacy,[unit]);assert q.snapshot()=={'completed':1}


def test_full_worker_does_not_take_pending_work(tmp_path,monkeypatch):
    import scripts.swarm_stream_work as w
    probes=[];prepared=[];updates=[];clock=[0]
    class Client:
        def __init__(self,*args):pass
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def capabilities(self):return True
        def call(self,op,*args):
            if op=='worker.policy':return {'accepting_jobs':True}
            probes.append(op)
            return {'available_cpus':0 if len(probes)==1 else 1,'available_mb':256}
        def run(self,spec,progress=lambda row:None):return {'state':'completed'}
    monkeypatch.setattr(w,'StreamClient',Client)
    monkeypatch.setattr(w.time,'sleep',lambda *a:clock.__setitem__(0,clock[0]+6))
    monkeypatch.setattr(w.time,'monotonic',lambda:clock[0])
    def prepare(unit,host):
        assert len(probes)==2;prepared.append(unit['id']);return unit
    assert w.distribute([{'id':'one'}],[{'host':'node'}],prepare,tmp_path/'legacy.sqlite',minimum_resources={'cpus':1,'memory_mb':256},progress=lambda row:updates.append(row))=={'completed':1}
    assert prepared==['one']
    assert updates[:2]==[{'queued':1},{'queued':1}]
    assert updates[-1]=={'completed':1}
