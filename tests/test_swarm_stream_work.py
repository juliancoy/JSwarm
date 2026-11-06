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
        def call(self,*args):return {'accepting_jobs':True,'available_cpus':2,'available_mb':512}
        def submit(self,spec):calls.append((self.host['host'],spec['id']))
        def status(self,ident):return {'state':'completed'}
    monkeypatch.setattr(w,'StreamClient',Client)
    def prepare(unit,host):prepared.append(unit['id']);return {'id':'remote-'+unit['id']}
    assert w.distribute(units,[{'host':'node'}],prepare,path)=={'completed':2}
    assert calls==[('node','remote-one'),('node','remote-two')] and prepared==['two']


def test_recovers_all_jobs_without_fixed_slots(tmp_path,monkeypatch):
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
        def call(self,*args):return {'accepting_jobs':False}
        def submit(self,spec):self.jobs.append(spec['id'])
        def status(self,ident):
            assert self.jobs==['one','two']
            return {'state':'completed'}
    monkeypatch.setattr(w,'StreamClient',Client)
    assert w.distribute(units,[{'host':'node'}],lambda *a:pytest.fail('Accepted work must not be prepared again'),path)=={'completed':2}
    assert len(clients)==1


def test_assigns_fitting_jobs_while_existing_jobs_are_running(tmp_path,monkeypatch):
    import scripts.swarm_stream_work as w
    submitted=[]
    class Client:
        def __init__(self,*args):pass
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def capabilities(self):return True
        def call(self,op):
            if op=='worker.policy':return {'accepting_jobs':True}
            return {'available_cpus':4-len(submitted),'available_mb':1024-256*len(submitted)}
        def submit(self,spec):submitted.append(spec['id'])
        def status(self,ident):return {'state':'running' if len(submitted)<3 else 'completed'}
    monkeypatch.setattr(w,'StreamClient',Client)
    result=w.distribute([{'id':str(i)} for i in range(3)],[{'host':'node','cpus':1}],lambda u,h:u,tmp_path/'queue.sqlite')
    assert result=={'completed':3} and len(submitted)==3


def test_pending_admissions_reserve_stale_capacity(tmp_path,monkeypatch):
    import scripts.swarm_stream_work as w
    submitted=[];polls={}
    class Client:
        def __init__(self,*args):pass
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def capabilities(self):return True
        def call(self,op):
            if op=='worker.policy':return {'accepting_jobs':True}
            return {'available_cpus':1,'available_mb':256}
        def submit(self,spec):
            if submitted:assert polls[submitted[-1]]>=3
            submitted.append(spec['id'])
        def status(self,ident):
            polls[ident]=polls.get(ident,0)+1
            return {'state':'queued' if polls[ident]<3 else 'completed'}
    monkeypatch.setattr(w,'StreamClient',Client)
    monkeypatch.setattr(w.time,'sleep',lambda _:None)
    assert w.distribute([{'id':'one'},{'id':'two'}],[{'host':'node'}],lambda u,h:u,tmp_path/'q')=={'completed':2}
    assert submitted==['one','two']


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
        def submit(self,spec):pass
        def status(self,ident):return {'state':'completed'}
    monkeypatch.setattr(w,'StreamClient',Client)
    monkeypatch.setattr(w.time,'sleep',lambda *a:clock.__setitem__(0,clock[0]+6))
    monkeypatch.setattr(w.time,'monotonic',lambda:clock[0])
    def prepare(unit,host):
        assert len(probes)==2;prepared.append(unit['id']);return unit
    assert w.distribute([{'id':'one'}],[{'host':'node'}],prepare,tmp_path/'legacy.sqlite',minimum_resources={'cpus':1,'memory_mb':256},progress=lambda row:updates.append(row))=={'completed':1}
    assert prepared==['one']
    assert updates[:2]==[{'queued':1},{'queued':1}]
    assert updates[-1]=={'completed':1}


@pytest.mark.parametrize('state,error', [('failed', 'failed'), ('failed_integrity', 'integrity'), ('completed', 'identity')])
def test_nonblocking_status_checks_terminal_results(state,error):
    from scripts.swarm_stream_work import WorkFailure
    class Transport:
        def call(self,*args):
            return {'state':state,'error':error,'contract_sha256':'correct',
                    'result':{'contract_sha256':'wrong','worker_machine':'worker'}}
    client=StreamClient({'machine':'worker'},client=Transport())
    with pytest.raises(WorkFailure if state=='failed' else IntegrityFailure):client.status('one')


def test_uncertain_submission_recovers_persisted_request(tmp_path,monkeypatch):
    import json
    import scripts.swarm_stream_work as w
    path=tmp_path/'q';units=[{'id':'one'}];stopped=[False];submitted=[]
    spec={'id':'remote-one','resources':{'cpus':2,'memory_mb':512}}
    class Client:
        def __init__(self,*args):pass
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def capabilities(self):return True
        def call(self,op):return {'accepting_jobs':True,'available_cpus':2,'available_mb':512}
        def submit(self,request):
            submitted.append(request)
            if len(submitted)==1:
                stopped[0]=True
                raise InterruptedError('Lost acknowledgement during shutdown')
        def status(self,ident):return {'state':'completed'}
    monkeypatch.setattr(w,'StreamClient',Client)
    assert w.distribute(units,[{'host':'node'}],lambda *a:spec,path,stopped=lambda:stopped[0])=={'accepted':1}
    row=FleetQueue(path,units).recover('node')[0]
    request=json.loads(row['request'])
    assert request['spec']==spec and request['admission_pending']
    stopped[0]=False
    assert w.distribute(units,[{'host':'node'}],lambda *a:pytest.fail('Must reuse accepted request'),path)=={'completed':1}
    assert submitted==[spec,spec]


def test_failed_job_does_not_disable_assignment_for_host(tmp_path,monkeypatch):
    import scripts.swarm_stream_work as w
    submitted=[]
    class Client:
        def __init__(self,*args):pass
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def capabilities(self):return True
        def call(self,op):return {'accepting_jobs':True,'available_cpus':1,'available_mb':256}
        def submit(self,spec):submitted.append(spec['id'])
        def status(self,ident):
            if ident=='one':raise w.WorkFailure('Task failed')
            return {'state':'completed'}
    monkeypatch.setattr(w,'StreamClient',Client)
    assert w.distribute([{'id':'one'},{'id':'two'}],[{'host':'node'}],lambda u,h:u,tmp_path/'q')=={'failed':1,'completed':1}
    assert submitted==['one','two']
