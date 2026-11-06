import importlib.util
import json
import sqlite3
from pathlib import Path


def test_sampler_preserves_start_and_separates_attempts(tmp_path,monkeypatch):
    source=Path(__file__).parents[1]/'scripts/shard_runtime_sampler.py'
    spec=importlib.util.spec_from_file_location('shard_runtime_sampler',source)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    monkeypatch.setattr('shard_cpu_accounting.collect',lambda reports:[])
    module.ROOT=tmp_path/'reports';module.DB=tmp_path/'stats.sqlite'
    folder=module.ROOT/'generation/full';folder.mkdir(parents=True)
    path=folder/'SRR-one.json'
    path.write_text(json.dumps({'container':'first','host':'node','phase':'running','started':'2026-11-03T12:00:00+00:00'}))
    module.sample()
    path.write_text(json.dumps({'container':'first','host':'node','phase':'complete','finished':'2026-11-03T12:02:00+00:00'}))
    module.sample()
    path.write_text(json.dumps({'container':'retry','host':'node','phase':'complete'}))
    module.sample()
    with sqlite3.connect(module.DB) as db:
        rows={container:json.loads(report) for container,report in db.execute('SELECT container,report FROM observations')}
    assert rows['first']['started']=='2026-11-03T12:00:00+00:00'
    assert rows['first']['finished']=='2026-11-03T12:02:00+00:00'
    assert 'started' not in rows['retry']


def test_stream_backfill_is_separate_bounded_and_refreshes_active_attempts(tmp_path):
    import importlib.util
    spec=importlib.util.spec_from_file_location('stream_sampler',Path(__file__).parents[1]/'scripts/shard_runtime_sampler.py')
    m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
    m.DB=tmp_path/'runtime';m.QUEUE=tmp_path/'queue'
    with sqlite3.connect(m.QUEUE) as db:
        db.execute('CREATE TABLE submissions(namespace,id,host,task_id,state,result)')
        db.executemany('INSERT INTO submissions(namespace,id,host,task_id,state) VALUES(?,?,?,?,?)',[
            ('stream:one','done','node','qc-done','completed'),('stream:one','live','node','qc-live','accepted')])
    calls=[]
    def fetch(host,ids):
        calls.append(ids)
        return {i:{'attempt':2,'state':'completed' if i=='qc-done' else 'running','elapsed_seconds':30,'started':'2026-11-03T12:00:00+00:00','finished':'2026-11-03T12:00:30+00:00' if i=='qc-done' else None,'container':None,'resources':{'cpus':1,'memory_mb':256}} for i in ids}
    assert m.sample_streams(limit=1,fetch=fetch)==1
    assert calls[-1]==['qc-live']
    assert m.sample_streams(limit=100,fetch=fetch)==2
    assert m.sample_streams(limit=100,fetch=fetch)==1
    assert calls[-1]==['qc-live']
    with sqlite3.connect(m.QUEUE) as db:assert list(db.execute('SELECT state FROM submissions'))==[('completed',),('accepted',)]
    with sqlite3.connect(m.DB) as db:assert db.execute('SELECT count(*) FROM stream_observations').fetchone()[0]==2


def test_completed_receipt_keeps_timing_when_node_is_unavailable(tmp_path):
    spec=importlib.util.spec_from_file_location('receipt_sampler',Path(__file__).parents[1]/'scripts/shard_runtime_sampler.py')
    m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
    m.DB=tmp_path/'runtime';m.QUEUE=tmp_path/'queue'
    receipt={'id':'qc-done','state':'completed','execution':{'state':'completed','attempt':3,'elapsed_seconds':25,'started':'2026-11-03T12:00:00+00:00','finished':'2026-11-03T12:00:25+00:00'}}
    with sqlite3.connect(m.QUEUE) as db:
        db.execute('CREATE TABLE submissions(namespace,id,host,task_id,state,result)')
        db.execute('INSERT INTO submissions VALUES(?,?,?,?,?,?)',('stream:one','done','node','qc-done','completed',json.dumps(receipt)))
    def unavailable(*args):raise AssertionError('Completed receipt must not require a live worker')
    assert m.sample_streams(fetch=unavailable)==1
    with sqlite3.connect(m.DB) as db:
        retained=json.loads(db.execute('SELECT report FROM stream_observations').fetchone()[0])
    assert retained['elapsed_seconds']==25 and retained['attempt']==3


def test_sampler_retains_profile_context_with_its_original_attempt(tmp_path,monkeypatch):
    spec=importlib.util.spec_from_file_location('profile_sampler',Path(__file__).parents[1]/'scripts/shard_runtime_sampler.py')
    m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
    m.ROOT=tmp_path/'reports';m.DB=tmp_path/'runtime'
    folder=m.ROOT/'generation/full';folder.mkdir(parents=True)
    context={'schema':'alignment-memory.v1','batch_bases':1000000}
    (folder/'SRR-profile.json').write_text(json.dumps({'container':'original','host':'not-enrolled','phase':'running','profiling_context':context}))
    import shard_cpu_accounting
    monkeypatch.setattr(shard_cpu_accounting,'collect',lambda active:[('SRR-profile','original',{'memory_peak_bytes':123456,'memory_current_bytes':100000,'memory_swap_bytes':0})])
    m.sample()
    with sqlite3.connect(m.DB) as db:r=json.loads(db.execute('SELECT report FROM observations').fetchone()[0])
    assert r['profiling_context']==context and r['memory_peak_bytes']==123456 and r['memory_swap_bytes']==0


def test_followup_sampler_preserves_attempt_peaks_without_mutating_queue(tmp_path):
    spec=importlib.util.spec_from_file_location('followup_sampler',Path(__file__).parents[1]/'scripts/shard_runtime_sampler.py')
    m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
    m.QUEUE=tmp_path/'queue';m.DB=tmp_path/'runtime';folder=tmp_path/'attempt';folder.mkdir()
    report=folder/'execution.json';report.write_text(json.dumps({'phase':'running','host':'node','container':'container-one','memory_reservation_mb':1280}))
    with sqlite3.connect(m.QUEUE) as db:
        db.execute('CREATE TABLE submissions(namespace,id,host,state,request)')
        db.execute('INSERT INTO submissions VALUES(?,?,?,?,?)',('genomics-followup:rev','call-one','node','accepted',json.dumps({'directory':str(folder)})))
    def fetch(active):
        assert len(active)==1
        return [(active[0][0],'container-one',{'memory_current_bytes':1000000,'memory_peak_bytes':2000000,'memory_swap_bytes':0})]
    assert m.sample_followup(fetch,runtime_root=tmp_path/'view')==1
    report.write_text(json.dumps({'phase':'complete','host':'node','container':'container-one','elapsed_seconds':10}))
    m.sample_followup(lambda active:[],runtime_root=tmp_path/'view')
    with sqlite3.connect(m.DB) as db:
        row=json.loads(db.execute('SELECT report FROM observations').fetchone()[0]);assert row['memory_peak_bytes']==2000000 and row['elapsed_seconds']==10
    with sqlite3.connect(m.QUEUE) as db:assert db.execute('SELECT state FROM submissions').fetchone()[0]=='accepted'
    assert json.loads((tmp_path/'view/call-one.json').read_text())['producer_memory']['memory_peak_bytes']==2000000


def test_known_incorrect_helper_memory_is_invalidated_without_losing_valid_cpu(tmp_path):
    spec=importlib.util.spec_from_file_location('memory_migration',Path(__file__).parents[1]/'scripts/shard_runtime_sampler.py')
    m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m);m.DB=tmp_path/'runtime'
    old={'cpu_core_seconds':12,'memory_peak_bytes':999,'memory_measurement_basis':'container cgroup v2; file includes shmem, do not add them; shared cache owner charged separately'}
    valid={'cpu_core_seconds':5,'memory_peak_bytes':123,'memory_measurement_basis':'observed container cgroup v2 peak/current'}
    with sqlite3.connect(m.DB) as db:
        db.execute('CREATE TABLE observations(job,container,report)')
        db.executemany('INSERT INTO observations VALUES(?,?,?)',[('old','a',json.dumps(old)),('valid','b',json.dumps(valid))])
    assert m.invalidate_helper_memory()==1 and m.invalidate_helper_memory()==0
    with sqlite3.connect(m.DB) as db:rows={k:json.loads(v) for k,v in db.execute('SELECT job,report FROM observations')}
    assert rows['old']['cpu_core_seconds']==12 and 'memory_peak_bytes' not in rows['old']
    assert rows['valid']['memory_peak_bytes']==123


def test_finalizer_observes_workers_and_requires_verified_checkpoint(tmp_path):
    spec=importlib.util.spec_from_file_location('finalizer_sampler',Path(__file__).parents[1]/'scripts/shard_runtime_sampler.py')
    m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m);m.DB=tmp_path/'runtime'
    root=tmp_path/'finalize';gen='a'*12;run='SRR7097858-'+gen+'-000007'
    folder=root/'generations'/gen/'mate-prepared';folder.mkdir(parents=True)
    state={'generation':gen,'target_host':'latitude','phase':'bounded_mate_preparation','collected':{run:{}},'mate_prepared':{}}
    (root/'state.json').write_text(json.dumps(state))
    report={'host':'latitude','container':'worker','phase':'running','started':'2026-11-04T12:00:00+00:00','cpus':2,'memory_mb':32000,'memory_reservation_mb':1024,'command':'PRIVATE COMMAND'}
    path=folder/(run+'-execution.json');path.write_text(json.dumps(report))
    assert m.sample_finalizer(root,lambda active:[])==2
    with sqlite3.connect(m.DB) as db:row=json.loads(db.execute('SELECT report FROM finalizer_observations WHERE id=?',(run,)).fetchone()[0])
    assert row['status']=='running' and row['memory_reservation_mb']==1024 and 'command' not in row
    report.update(phase='complete',finished='2026-11-04T12:01:00+00:00');path.write_text(json.dumps(report))
    m.sample_finalizer(root,lambda active:[])
    with sqlite3.connect(m.DB) as db:row=json.loads(db.execute('SELECT report FROM finalizer_observations WHERE id=?',(run,)).fetchone()[0])
    assert row['status']=='preparing'  # Exit alone cannot claim verified science.
    state['mate_prepared'][run]={'sha256':'verified'};(root/'state.json').write_text(json.dumps(state))
    m.sample_finalizer(root,lambda active:[])
    with sqlite3.connect(m.DB) as db:row=json.loads(db.execute('SELECT report FROM finalizer_observations WHERE id=?',(run,)).fetchone()[0])
    assert row['status']=='completed' and row['verified']
def test_regional_extraction_telemetry_is_sampled_and_retained_after_completion(tmp_path):
    source=Path(__file__).parents[1]/'scripts/shard_runtime_sampler.py'
    spec=importlib.util.spec_from_file_location('regional_sampler',source)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    module.QUEUE=tmp_path/'queue';module.DB=tmp_path/'runtime'
    folder=tmp_path/'region';folder.mkdir();path=folder/'extraction.json'
    report={'phase':'running','host':'latitude','container':'extractor','started':'2026-11-05T12:00:00+00:00','image':'image','command':'PRIVATE'}
    path.write_text(json.dumps(report))
    with sqlite3.connect(module.QUEUE) as db:
        db.execute('CREATE TABLE submissions(namespace,id,host,state,request)')
        db.execute('INSERT INTO submissions VALUES(?,?,?,?,?)',('genomics-followup-inputs:rev','region-one','latitude','accepted',json.dumps({'directory':str(folder),'source_image':'image'})))
    def fetch(active):
        assert len(active)==1 and active[0][1]['container']=='extractor'
        return [(active[0][0],'extractor',{'cpu_core_seconds':9,'memory_current_bytes':123,'memory_peak_bytes':456})]
    assert module.sample_followup(fetch,runtime_root=tmp_path/'view')==1
    path.write_text(json.dumps({**report,'phase':'complete','finished':'2026-11-05T12:01:00+00:00','elapsed_seconds':60}))
    with sqlite3.connect(module.QUEUE) as db:db.execute("UPDATE submissions SET state='completed'")
    module.sample_followup(lambda active:[],runtime_root=tmp_path/'view')
    with sqlite3.connect(module.DB) as db:value=json.loads(db.execute('SELECT report FROM observations').fetchone()[0])
    assert value['elapsed_seconds']==60 and value['cpu_core_seconds']==9 and value['memory_peak_bytes']==456
    assert 'command' not in value

