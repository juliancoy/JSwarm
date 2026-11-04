"""A rolling coordinator update preserves ownership without draining the queue."""
import json
import sqlite3
from pathlib import Path
import sys
import threading

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'swarm-control/server'))
from submission_queue import SubmissionQueue


def test_fence_keeps_active_tokens_and_allows_successor_to_claim_immediately(tmp_path):
    old=SubmissionQueue('alignment',tmp_path/'queue')
    old.submit_many([{'id':str(i)} for i in range(3)])
    a=old.claim('host#cpu');old.update(a['id'],a['token'],state='accepted')
    b=old.claim('host#gpu')  # preparation in flight is protected too
    new=SubmissionQueue('alignment',old.path,epoch='abcdef012345')
    saved=new.activate_epoch()
    assert {r['token'] for r in saved}=={a['token'],b['token']}
    new.recover_preparations(exclude={r['id'] for r in saved})
    # Legacy SQL, with no awareness of epochs, is fenced by the database.
    with old.connect() as db:
        try:
            db.execute("UPDATE submissions SET state='preparing',token='legacy' WHERE namespace='alignment' AND id='2'")
        except sqlite3.IntegrityError as e:assert 'superseded' in str(e)
        else:raise AssertionError('Legacy scheduler was allowed a new claim')
    try:old.adopt('2','host#cpu')
    except sqlite3.IntegrityError as e:assert 'superseded' in str(e)
    else:raise AssertionError('Legacy adoption bypassed the claim fence')
    c=new.claim('host#cpu')
    assert c['id']=='2' and c['token'].startswith('abcdef012345:')
    # Old owner finishes its accepted work using exactly the original token.
    old.update(a['id'],a['token'],state='completed',result=json.dumps({'verified':True}))
    old.update(b['id'],b['token'],state='accepted')
    assert [r['id'] for r in new.handoff_claims(active_only=True)]==[b['id']]
    old.update(b['id'],b['token'],state='completed')
    assert not new.handoff_claims(active_only=True)
    assert new.snapshot()=={'completed':2,'preparing':1}


def test_failure_can_requeue_without_fencing_other_workflows(tmp_path):
    old=SubmissionQueue('alignment',tmp_path/'queue');old.submit({'id':'a'})
    a=old.claim('host');old.update('a',a['token'],state='accepted')
    new=SubmissionQueue('alignment',old.path,epoch='abcdef012345');new.activate_epoch()
    old.update('a',a['token'],state='queued')
    assert not new.handoff_claims(active_only=True)
    claimed=new.claim('host');assert claimed['token']!=a['token']
    other=SubmissionQueue('qc',old.path);other.submit({'id':'qc'})
    assert other.claim('host')['id']=='qc'


def test_overlapping_rollouts_do_not_retire_earlier_owners_prematurely(tmp_path):
    old=SubmissionQueue('alignment',tmp_path/'queue');old.submit_many([{'id':'a'},{'id':'b'}])
    a=old.claim('host');old.update('a',a['token'],state='accepted')
    first=SubmissionQueue('alignment',old.path,epoch='aaaaaaaaaaaa');first.activate_epoch()
    b=first.claim('host');first.update('b',b['token'],state='accepted')
    second=SubmissionQueue('alignment',old.path,epoch='bbbbbbbbbbbb');second.activate_epoch()
    assert len(first.handoff_claims(active_only=True))==1
    assert len(second.handoff_claims(active_only=True))==2
    old.update('a',a['token'],state='completed')
    assert not first.handoff_claims(active_only=True)
    assert [r['id'] for r in second.handoff_claims(active_only=True)]==['b']


def test_worker_can_finish_while_successor_schedules_new_work(tmp_path):
    old=SubmissionQueue('alignment',tmp_path/'queue');old.submit_many([{'id':'active'},{'id':'new'}])
    a=old.claim('host');old.update(a['id'],a['token'],state='accepted')
    finish=threading.Event();done=[]
    def worker():
        finish.wait(5)
        old.update(a['id'],a['token'],state='completed');done.append(a['token'])
    thread=threading.Thread(target=worker);thread.start()
    try:
        new=SubmissionQueue('alignment',old.path,epoch='abcdef012345');new.activate_epoch()
        assert new.claim('host')['id']=='new'
        assert not done  # queue is live before the previous job has finished
        finish.set();thread.join(5)
        assert done==[a['token']]
    finally:finish.set();thread.join(5)


def test_deploy_retains_previous_process_and_does_not_stop_workers(tmp_path,monkeypatch):
    from types import SimpleNamespace
    from scripts import alignment_rollout as rollout,vulkan_alignment_deployment as deployment
    folder=tmp_path/'recall/venter';folder.mkdir(parents=True)
    (folder/'generation.json').write_text(json.dumps({'generation':'012345abcdef'}))
    queue=SubmissionQueue('venter-alignment:012345abcdef',tmp_path/'q')
    queue.submit({'id':'active'});a=queue.claim('host');queue.update('active',a['token'],state='accepted')
    monkeypatch.setattr(rollout,'SubmissionQueue',lambda namespace,epoch:SubmissionQueue(namespace,queue.path,epoch=epoch))
    calls=[]
    def docker(*args,**kwargs):
        calls.append(args)
        if args[0]=='inspect':
            return SimpleNamespace(returncode=0,stdout=json.dumps([{'Config':{'Labels':{'organization.managed-by':'run.py','org.lifetech.service':'alignment-controller'}},'State':{'Running':True,'Health':{'Status':'healthy'}}}]))
        return SimpleNamespace(returncode=0,stdout='')
    monkeypatch.setattr(deployment,'docker',docker)
    monkeypatch.setattr(deployment,'command',lambda image,root,epoch:['run','-d','--name',deployment.NAME,'--env','SWARM_CONTROLLER_EPOCH='+epoch,image])
    system=[]
    monkeypatch.setattr(rollout.subprocess,'run',lambda args,**kw:(system.append(args) or SimpleNamespace(returncode=3)))
    assert rollout.deploy(tmp_path,'new-image','worker',{}, {})==0
    assert not any(c[0] in ('stop','kill','rm') for c in calls)
    assert any(c[0]=='rename' for c in calls)
    assert old_token(queue,'active')==a['token']
    assert any(c[0]=='systemd-run' for c in system)


def old_token(queue,ident):
    with queue.connect() as db:return db.execute('SELECT token FROM submissions WHERE id=?',(ident,)).fetchone()[0]


def test_node_journal_recovery_fences_previous_coordinator_without_restarting_job(tmp_path):
    old=SubmissionQueue('alignment',tmp_path/'q');old.submit({'id':'running-node-job'})
    claim=old.claim('node#gpu');old.update(claim['id'],claim['token'],state='accepted')
    new=SubmissionQueue('alignment',old.path,epoch='abcdef012345');new.activate_epoch()
    recovered=new.adopt(claim['id'],'node#gpu')
    assert recovered['token']!=claim['token'] and recovered['state']=='accepted'
    import pytest
    with pytest.raises(RuntimeError,match='Stale'):
        old.update(claim['id'],claim['token'],state='queued')
    new.update(claim['id'],recovered['token'],state='completed')
    assert new.snapshot()=={'completed':1}


def test_queue_priorities_persist_and_skip_nonfitting_work(tmp_path):
    q=SubmissionQueue('priorities',tmp_path/'queue')
    q.submit_many([{'id':'ordinary'},{'id':'large','priority':200,'resources':{'memory_mb':9999}}, {'id':'qualification','priority':100},{'id':'promoted'}])
    q.prioritize('promoted',150)
    q=SubmissionQueue('priorities',q.path)
    assert q.claim('host',{'available_mb':100})['id']=='promoted'
    assert q.claim('host',{'available_mb':100})['id']=='qualification'
    assert q.claim('host',{'available_mb':100})['id']=='ordinary'
    assert q.claim('host',{'available_mb':100}) is None



def test_timesharing_claim_does_not_subtract_cpu_startup_reservations(tmp_path):
    q=SubmissionQueue('timeshare',tmp_path/'queue')
    q.submit_many([{'id':str(i),'resources':{'cpus':4,'memory_mb':128}} for i in range(3)])
    capacity={'available_cpus':4,'available_mb':1024,'cpu_admission_mode':'timeshare'}
    estimates={str(i):{'cpus':4,'memory_mb':128} for i in range(3)}
    assert q.claim('host#cpu',capacity,requirements=estimates)
    assert q.claim('host#cpu',capacity,requirements=estimates)
    assert q.claim('host#cpu',capacity,requirements=estimates)
