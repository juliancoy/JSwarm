import importlib.util
import json
import sqlite3
from pathlib import Path


def test_sampler_preserves_start_and_separates_attempts(tmp_path):
    source=Path(__file__).parents[1]/'scripts/shard_runtime_sampler.py'
    spec=importlib.util.spec_from_file_location('shard_runtime_sampler',source)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
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
