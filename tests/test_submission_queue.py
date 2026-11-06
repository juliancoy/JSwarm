import concurrent.futures
import json
import sys
from pathlib import Path
import pytest
sys.path.insert(0,str((Path(__file__).resolve().parents[2] if Path(__file__).resolve().parents[1].name in ('GenomeHarbor', 'JSwarm') else Path(__file__).resolve().parents[1])/'swarm-control/server'))
from submission_queue import SubmissionQueue


def test_concurrent_workers_claim_each_job_once(tmp_path):
    path=tmp_path/'queue.db'
    q=SubmissionQueue('alignment',path)
    for n in range(100):q.submit({'id':str(n),'resources':{'cpus':1,'memory_mb':20}})
    def worker(n):
        own=SubmissionQueue('alignment',path);claimed=[]
        while row:=own.claim(str(n),{'available_cpus':2,'available_mb':100}):
            own.update(row['id'],row['token'],state='accepted')
            claimed.append(row['id'])
            own.update(row['id'],row['token'],state='completed',result='{}')
        return claimed
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:rows=sum(pool.map(worker,range(8)),[])
    assert len(rows)==len(set(rows))==100
    assert q.snapshot()=={'completed':100}


def test_fit_skips_large_and_gpu_jobs_and_honors_pause(tmp_path):
    q=SubmissionQueue('jobs',tmp_path/'q')
    q.submit({'id':'large','resources':{'memory_mb':10000}})
    q.submit({'id':'gpu','resources':{'sink':'gpu','cpus':1}})
    q.submit({'id':'cpu','resources':{'cpus':1,'memory_mb':128}})
    assert q.claim('paused',{'accepting_jobs':False}) is None
    assert q.claim('small',{'available_cpus':1,'available_mb':256})['id']=='cpu'
    assert q.claim('gpu',{'gpu':True,'available_mb':256})['id']=='gpu'
    assert q.claim('small',{'available_mb':256}) is None


def test_restart_keeps_accepted_and_fences_old_preparation(tmp_path):
    q=SubmissionQueue('jobs',tmp_path/'q')
    q.submit({'id':'one'});q.submit({'id':'two'})
    old=q.claim('one');accepted=q.claim('two')
    q.update('two',accepted['token'],state='accepted',task_id='launcher-id',request='{}')
    q=SubmissionQueue('jobs',tmp_path/'q');q.recover_preparations()
    new=q.claim('other');assert new['id']=='one' and new['token']!=old['token']
    with pytest.raises(RuntimeError):q.update('one',old['token'],state='completed')
    assert q.recover('two')[0]['task_id']=='launcher-id'
    assert q.claim('other') is None


def test_namespaces_share_database_without_id_collisions(tmp_path):
    a=SubmissionQueue('tests',tmp_path/'q');b=SubmissionQueue('dna',tmp_path/'q')
    a.submit({'id':'same','kind':'test'});b.submit({'id':'same','kind':'dna'})
    assert json.loads(a.claim('node')['spec'])['kind']=='test'
    assert json.loads(b.claim('node')['spec'])['kind']=='dna'
    with pytest.raises(ValueError):a.submit({'id':'same','kind':'changed'})


def test_release_revokes_claim_before_another_worker_can_take_it(tmp_path):
    q=SubmissionQueue('jobs',tmp_path/'q');q.submit({'id':'one'})
    row=q.claim('first');q.update('one',row['token'],state='queued',host=None)
    with pytest.raises(RuntimeError):q.update('one',row['token'],state='completed')
    assert q.claim('second')['token']!=row['token']


def test_bulk_submission_is_atomic_on_changed_input(tmp_path):
    q=SubmissionQueue('jobs',tmp_path/'q');q.submit({'id':'one'})
    with pytest.raises(ValueError):q.submit_many([{'id':'two'},{'id':'one','changed':True}])
    assert q.snapshot()=={'queued':1}


def test_placement_specific_memory_prevents_ram_only_false_claim_and_allows_mmap(tmp_path):
    q=SubmissionQueue('alignment',tmp_path/'q')
    q.submit({'id':'one','resources':{'cpus':2,'memory_mb':8192}})
    assert q.claim('ram-only',{'available_mb':11000,'available_cpus':7},requirements={'one':{'cpus':2,'memory_mb':11788}}) is None
    row=q.claim('mmap-ssd',{'available_mb':5000,'available_cpus':7},requirements={'one':{'cpus':2,'memory_mb':4096}})
    assert row['admission_resources']['memory_mb']==4096
    assert json.loads(row['spec'])['resources']['memory_mb']==8192


def test_placement_estimates_cannot_remove_gpu_capability_requirements(tmp_path):
    q=SubmissionQueue('jobs',tmp_path/'q')
    q.submit({'id':'gpu','resources':{'sink':'gpu','cpus':2,'memory_mb':8192,'capabilities':['qualified']}})
    assert q.claim('cpu',{'available_mb':5000,'available_cpus':7},requirements={'gpu':{'memory_mb':4096}}) is None
    assert q.claim('unqualified',{'gpu':True,'available_mb':5000,'available_cpus':7},requirements={'gpu':{'memory_mb':4096}}) is None
    with pytest.raises(ValueError):q.claim('bad',requirements={'gpu':{'memory_mb':-1}})
    with pytest.raises(ValueError):q.claim('bad',requirements={'gpu':{'sink':'cpu'}})
    assert q.claim('qualified',{'gpu':True,'capabilities':['qualified'],'available_mb':5000,'available_cpus':7},requirements={'gpu':{'memory_mb':4096}})['id']=='gpu'


def test_pending_admission_reserves_physical_host_across_lanes_and_namespaces(tmp_path):
    path=tmp_path/'q'
    a=SubmissionQueue('dna',path);b=SubmissionQueue('other',path)
    a.submit({'id':'one','resources':{'cpus':2,'memory_mb':8192}})
    b.submit({'id':'two','resources':{'cpus':2,'memory_mb':8192}})
    capacity={'available_cpus':8,'available_mb':12000}
    first=a.claim('node#cpu',capacity,requirements={'one':{'memory_mb':8192}})
    assert first
    assert capacity=={'available_cpus':8,'available_mb':12000}
    assert b.claim('node#gpu',capacity,requirements={'two':{'memory_mb':8192}}) is None
    a.update('one',first['token'],state='accepted',request=json.dumps({'admission_pending':False,'resources':first['admission_resources']}))
    assert b.claim('node#gpu',{'available_cpus':6,'available_mb':3808},requirements={'two':{'memory_mb':8192}}) is None
    a.update('one',first['token'],state='completed')
    assert b.claim('node#cpu',capacity,requirements={'two':{'memory_mb':8192}})


def test_input_preparation_does_not_reserve_future_compute_memory(tmp_path):
    q=SubmissionQueue('jobs',tmp_path/'q');budget={'cpus':2,'memory_mb':4096}
    for ident in ('a','b','c'):q.submit({'id':ident,'resources':budget})
    cap={'available_cpus':0,'available_mb':256}
    a=q.claim('peer',cap,requirements={'a':budget},eligible=['a'],preparation_resources={'cpus':0,'memory_mb':128})
    assert a['admission_resources']==budget
    b=q.claim('peer',cap,requirements={'b':budget},eligible=['b'],preparation_resources={'cpus':0,'memory_mb':128})
    assert b
    assert q.claim('peer',cap,requirements={'c':budget},eligible=['c'],preparation_resources={'cpus':0,'memory_mb':128}) is None
    with q.connect() as db:r=json.loads(db.execute("SELECT request FROM submissions WHERE id='a'").fetchone()[0])
    r['inputs_ready']=True;q.update('a',a['token'],state='accepted',request=json.dumps(r))
    assert q.claim('peer',{'available_cpus':4,'available_mb':4096},requirements={'c':budget},eligible=['c'],preparation_resources={'cpus':0,'memory_mb':128}) is None
