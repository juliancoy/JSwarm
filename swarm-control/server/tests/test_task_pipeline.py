import json
import sqlite3
from task_pipeline import snapshot


def test_projection_preserves_shards_and_claim_semantics_without_exposing_commands(tmp_path):
    path=tmp_path/'q';db=sqlite3.connect(path)
    db.execute('CREATE TABLE submissions(namespace,id,spec,state,host,created,updated,request,error,result)')
    spec={'shard':7,'kind':'paired-read-alignment','resources':{'cpus':2},'depends_on':['reference'],'command':['secret-command']}
    db.execute('INSERT INTO submissions VALUES(?,?,?,?,?,?,?,?,?,?)',('dna:g','shard7',json.dumps(spec),'accepted','node#cpu',1,2,json.dumps({'admission_pending':True}),None,None))
    db.commit();db.close()
    row=snapshot(path)['items'][0]
    assert row['status']=='waiting_capacity' and row['queue_state']=='accepted'
    assert row['parent']=='dna:g' and row['dependencies']==['reference'] and row['shard']==7
    assert 'secret-command' not in json.dumps(row)
    with sqlite3.connect(path) as db:assert db.execute('SELECT state FROM submissions').fetchone()[0]=='accepted'


def test_missing_queue_returns_empty(tmp_path):
    assert snapshot(tmp_path/'missing')['items']==[]


def test_tests_have_one_latest_state_and_preserve_attempt_history():
    from task_pipeline import collapse_test_attempts
    def item(run, task, state, created):
        return dict(id=f'application-tests:{run}/{task}', parent=f'application-tests:{run}',
                    job_id=task, kind='application-test', status=state, queue_state=state,
                    created=created, updated=created)
    old=item('old','a','failed',1)
    passed=item('fix','a','completed',2)
    unrelated=item('other','b','failed',1)
    rows=collapse_test_attempts([passed,unrelated,old])
    assert len(rows)==2
    current=next(row for row in rows if row['job_id']=='a')
    assert current['id']=='application-test/a'
    assert current['status']=='completed'
    assert [attempt['status'] for attempt in current['attempts']]==['completed','failed']
    queued=item('rerun','a','queued',3)
    assert collapse_test_attempts([passed,old,queued])[0]['status']=='queued'
    assert old['status']=='failed'


def test_non_test_shards_remain_independent():
    from task_pipeline import collapse_test_attempts
    rows=[dict(id='a/shard',job_id='shard',kind='alignment'),
          dict(id='b/shard',job_id='shard',kind='alignment')]
    assert collapse_test_attempts(rows)==rows


def test_resources_use_current_worker_report_without_overwriting_submission_estimate(tmp_path):
    from submission_queue import SubmissionQueue
    generation='2f810a09136e';ident='SRR7097858-'+generation+'-000007'
    path=tmp_path/'q';queue=SubmissionQueue('venter-alignment:'+generation,path)
    queue.submit({'id':ident,'kind':'paired-read-alignment','resources':{'cpus':2,'memory_mb':8192}})
    claim=queue.claim('mini#cpu',requirements={ident:{'cpus':2,'memory_mb':4096}})
    queue.update(ident,claim['token'],state='accepted',request=json.dumps({'admission_pending':False,'resources':{'cpus':2,'memory_mb':4096}}))
    folder=tmp_path/'recall/venter/generations'/generation/'full';folder.mkdir(parents=True)
    report=folder/(ident+'.json')
    report.write_text(json.dumps({'host':'mini','phase':'running','container':'worker','cpus':7,'memory_mb':4096,'command':'export LIFETECH_BWA_SHARED=1; bwa mem'}))
    row=snapshot(path,tmp_path)['items'][0]
    assert row['status']=='running'
    assert row['submission_resources']['memory_mb']==8192
    assert row['placement_resources']=={'cpus':2,'memory_mb':4096}
    assert row['worker_resources']=={'cpus':7,'memory_mb':4096}
    assert row['reference_mode']=='shared-index'
    report.write_text(json.dumps({'host':'other','phase':'running','container':'foreign','memory_mb':1000}))
    assert snapshot(path,tmp_path)['items'][0]['worker_resources'] is None
    queue.update(ident,claim['token'],state='queued',host=None)
    row=snapshot(path,tmp_path)['items'][0]
    assert row['worker_resources'] is None and row['placement_resources'] is None
    assert row['resources']['memory_mb']==8192


def test_completed_peak_is_a_measurement_not_a_reservation(tmp_path):
    from submission_queue import SubmissionQueue
    path=tmp_path/'q';queue=SubmissionQueue('jobs',path)
    queue.submit({'id':'one','resources':{'memory_mb':8192}})
    claim=queue.claim('node')
    queue.update('one',claim['token'],state='completed',result=json.dumps({'peak_memory_bytes':3*1024**3,'reference_transport':'nfs-fscache'}))
    row=snapshot(path)['items'][0]
    assert row['peak_memory_bytes']==3*1024**3
    assert row['worker_resources'] is None
    assert row['reference_mode']=='nfs-fscache'


def test_runtime_observation_does_not_use_queue_age():
    from task_pipeline import runtime_observation
    assert runtime_observation(None,{},'node')['elapsed_seconds'] is None
    observed=runtime_observation({'started':'2026-11-03T12:00:00+00:00','finished':'2026-11-03T12:02:00+00:00','gpu':True}, {'reference_transport':'nfs-fscache'}, 'node#gpu')
    assert observed['elapsed_seconds']==120
    assert observed['technologies']==['GPU (scope unreported)','FS-CACHE reference']


def test_forecast_reports_missing_coverage_and_uses_completion_throughput():
    from task_pipeline import runtime_summary
    items=[{'parent':'alignment','status':'completed','updated':t} for t in (100,200,300)]
    items += [{'parent':'alignment','status':'queued','updated':100}, {'parent':'finalize','status':'queued','updated':100}]
    result=runtime_summary(items,now=350)
    assert result['estimated_seconds']==100
    assert not result['complete_coverage']
    assert result['unestimated_workflows']==['finalize']


def test_io_accounting_preserves_unknowns_and_groups_verified_endpoints():
    from task_pipeline import io_observation
    unknown=io_observation(None,{},'node','running')
    assert unknown['read_bytes'] is None and unknown['network_write_bytes'] is None
    data={'state':'aligning','read':[{'endpoint':'source','bytes':30,'network':True},
          {'endpoint':'source','bytes':20,'network':True},{'endpoint':'worker','bytes':10,'network':False},
          {'endpoint':'bad','bytes':True},{'endpoint':'bad','bytes':-1}], 'write':[]}
    observed=io_observation(data,{},'worker','running')
    assert observed['read_bytes']==60 and observed['network_read_bytes']==50
    assert observed['write_bytes']==0 and len(observed['read'])==2
    old=io_observation(None,{'host':'source','executor':'worker','outputs':{'bam':{'bytes':100}}},'worker','completed')
    assert old['read_bytes'] is None and old['write_bytes']==100 and old['network_write_bytes']==100


def test_io_sidecar_must_match_current_container_and_worker(tmp_path):
    from task_pipeline import execution_report
    gen='2f810a09136e';ident='SRR7097858-'+gen+'-000007'
    folder=tmp_path/'recall/venter/generations'/gen/'full';folder.mkdir(parents=True)
    (folder/(ident+'.json')).write_text(json.dumps({'host':'worker','container':'current','phase':'running'}))
    sidecar=folder/(ident+'.io.json')
    sidecar.write_text(json.dumps({'worker':'worker','container':'old','read':[]}))
    assert execution_report('venter-alignment:'+gen,ident,'worker',tmp_path)['io'] is None
    sidecar.write_text(json.dumps({'worker':'worker','container':'current','read':[]}))
    assert execution_report('venter-alignment:'+gen,ident,'worker',tmp_path)['io']['read']==[]


def test_run_types_use_declared_recipe_and_completion_time_uses_real_finish():
    from task_pipeline import run_type,completion_time
    assert run_type({}, {'recipe':'fastq.gpu-qc.v1'},'stream:opaque')=='genetic-qc'
    assert run_type({'kind':'paired-read-alignment'}, {},'workflow')=='mapping'
    assert run_type({'kind':'application-test'}, {},'workflow')=='web'
    assert run_type({}, {},'stream:unknown')=='other'
    assert completion_time(None,{'result':{'finished':50}},100)==50
    assert completion_time({'finished':'2026-11-03T00:00:00+00:00'},{},100)>100
    assert completion_time(None,{},100)==100


def test_technology_labels_distinguish_gpu_alignment_and_reference_sharing():
    from task_pipeline import runtime_observation
    gpu=runtime_observation({'gpu':True},{'alignment_backend':{'backend':'bwa-vulkan-exact','gpu':True}},'node')
    assert 'GPU alignment (CPU seeding)' in gpu['technologies']
    assert not any('VRAM' in label for label in gpu['technologies'])
    ram=runtime_observation({'shared_index':True,'shared_reference_ram':True}, {},'node')
    assert ram['technologies']==['Shared BWA index (host RAM)']
    mapped=runtime_observation({'shared_index':True},{'reference_transport':'nfs-fscache'},'node')
    assert mapped['technologies']==['FS-CACHE reference','Shared BWA index (file mapping)']
    assert runtime_observation(None,{'reference_transport':'node-shared-ram'},'node')['technologies']==['Shared BWA index (host RAM)']


def test_pressure_failures_explain_experimental_scope_without_turning_them_into_passes():
    from task_pipeline import failure_detail
    note=failure_detail({'kind':'memory-pressure-benchmark'}, {'timed_out':True,'limit_mib':4096,'wall_seconds':902},'failed')
    assert '4096 MiB' in note and '902s' in note and 'inconclusive' in note
    assert failure_detail({'kind':'paired-read-alignment'}, {'timed_out':True},'failed') is None


def test_completed_pressure_measurement_does_not_claim_alignment_success():
    from task_pipeline import failure_detail
    note=failure_detail({'kind':'memory-pressure-benchmark'},{'outcome':'memory-pressure-timeout'},'completed')
    assert 'alignment did not finish' in note and 'measurement completed' in note


def test_superseded_benchmark_attempts_keep_failure_in_history(tmp_path):
    from task_pipeline import archive_benchmark_attempts,runtime_summary
    path=tmp_path/'qc/full-shard-memory-pressure/latest.json';path.parent.mkdir(parents=True)
    path.write_text(json.dumps({'id':'20261104T020244Z-53684a','phase':'benchmarking','shard':3000,'target':'mini'}))
    old={'kind':'memory-pressure-benchmark','parent':'memory-pressure:old','shard':3000,'worker':'mini','queue_state':'failed','status':'failed'}
    actual={'kind':'application-test','parent':'web','shard':None,'worker':'mini','queue_state':'failed','status':'failed'}
    current={**old,'parent':'memory-pressure:20261104T020244Z-53684a'}
    archive_benchmark_attempts([old,actual,current],tmp_path)
    assert old['status']=='failed' and old['queue_state']=='failed'
    assert old['superseded_by']==current['parent']
    assert 'superseded_by' not in actual and 'superseded_by' not in current
    assert runtime_summary([old])['remaining_jobs']==0


def test_gpu_decoration_uses_actual_qc_work_not_requested_recipe_or_fallback():
    from task_pipeline import runtime_observation
    gpu={'result':{'outputs':{'gpu_bases':1000,'histogram_source':'vulkan','gpu_cpu_crosschecked_batches':2,'gpu_devices':['AMD Radeon Graphics (RADV RENOIR)']}}}
    observed=runtime_observation(None,gpu,'mini')
    assert observed['gpu_used'] is True
    assert observed['gpu_devices']==['AMD Radeon Graphics (RADV RENOIR)']
    assert observed['technologies']==['GPU quality control','Vulkan','CPU cross-check']
    fallback=runtime_observation(None,{'result':{'outputs':{'gpu_bases':0,'cpu_bases':1000}}},'mini')
    assert fallback['gpu_used'] is False
    assert runtime_observation(None,{},'mini#gpu')['gpu_used'] is False


def test_cpu_core_time_uses_counter_timestamp_not_dashboard_poll_time():
    from task_pipeline import runtime_observation
    report={'started':'2026-01-01T00:00:00+00:00','cpu_sampled_at':1767225610,'cpu_core_seconds':20}
    value=runtime_observation(report,{},'test#cpu')
    assert value['cpu_core_seconds']==20
    assert value['average_cores']==2
    assert runtime_observation({'cpu_core_seconds':-1},{},'test')['cpu_core_seconds'] is None


def test_qc_estimator_learns_retained_node_attempt_times_without_queue_age(tmp_path):
    from submission_queue import SubmissionQueue
    queue=tmp_path/'queue';runtime=tmp_path/'runtime';ns='stream:quality'
    q=SubmissionQueue(ns,queue)
    specs=[{'id':str(i),'reads':500000,'shard':i} for i in range(4)]
    q.submit_many(specs)
    with q.connect() as db:
        for i in range(4):
            db.execute('UPDATE submissions SET state=?,host=?,task_id=?,request=?,result=?,created=1,updated=10000000000 WHERE namespace=? AND id=?',
                ('completed' if i<3 else 'accepted','node','qc-'+str(i),json.dumps({'recipe':'fastq.gpu-qc.v1','resources':{'cpus':1,'memory_mb':256}}),
                 json.dumps({'result':{'outputs':{'gpu_bases':50000000}}}),ns,str(i)))
    with sqlite3.connect(runtime) as db:
        db.execute('CREATE TABLE stream_observations(namespace,id,host,task_id,report)')
        for i in range(3):
            report={'phase':'complete','container':None,'started':'2026-11-03T12:00:00+00:00','finished':'2026-11-03T12:00:30+00:00','elapsed_seconds':30,
                    'resources':{'cpus':1,'memory_mb':256},'timing_basis':'node transition journal','recorded_at':1793707230}
            db.execute('INSERT INTO stream_observations VALUES(?,?,?,?,?)',(ns,str(i),'node','qc-'+str(i),json.dumps(report)))
    value=snapshot(queue,tmp_path,runtime)
    assert value['runtime_inference']['timed_samples']==3
    pending=next(row for row in value['items'] if row['job_id']=='3')
    assert pending['runtime']['elapsed_seconds'] is None
    assert pending['runtime_estimate']['typical_seconds']==30
    completed=next(row for row in value['items'] if row['job_id']=='0')
    assert completed['runtime']['timing_basis']=='node transition journal'
    # A moved assignment cannot borrow the former worker's execution timing.
    with q.connect() as db:db.execute("UPDATE submissions SET host='another' WHERE id='0'")
    assert next(row for row in snapshot(queue,tmp_path,runtime)['items'] if row['job_id']=='0')['runtime']['elapsed_seconds'] is None


def test_arrival_uses_latest_concurrent_workflow_and_reports_range():
    from task_pipeline import runtime_summary
    items=[]
    for workflow,times in [('a',(100,200,300)),('b',(100,150,200))]:
        items += [{'parent':workflow,'status':'completed','updated':t} for t in times]
        items.append({'parent':workflow,'status':'queued','updated':300})
    result=runtime_summary(items,now=350)
    assert result['estimated_seconds']==100
    assert result['arrival_at']==450
    assert result['arrival_low_at']<=result['arrival_at']<=result['arrival_high_at']
    assert result['complete_coverage'] and result['estimated_jobs']==2


def test_failed_work_does_not_receive_an_automatic_retry_arrival():
    from task_pipeline import runtime_summary
    items=[{'parent':'a','status':'completed','updated':t} for t in (100,200,300)]
    items.append({'parent':'a','status':'failed','updated':310})
    result=runtime_summary(items,now=350)
    assert result['arrival_at'] is None
    assert result['blocked_jobs']==1 and not result['complete_coverage']
    assert result['unestimated_workflows']==['a']



def test_enrollment_inventory_includes_idle_nodes_without_polling(tmp_path,monkeypatch):
    import task_pipeline as module
    config=tmp_path/'peers.json';config.write_text(json.dumps({'peers':{'julian-minipc.local':'10.0.0.2','compai.local':'10.0.0.1'}}))
    assert module.enrolled_machines(config)==['compai.local','julian-minipc.local']
    monkeypatch.setattr(module,'enrolled_machines',lambda:['julian-minipc.local'])
    assert module.snapshot(tmp_path/'no-work')['machines']==['julian-minipc.local']


def test_vulkan_comparison_receipt_marks_actual_gpu_execution():
    from task_pipeline import runtime_observation,gpu_execution_metadata
    receipt={'execution_backend':'vulkan','method':'comparison','gpu_qualification':'qualified'}
    observed=runtime_observation(None,receipt,'compai.local')
    assert observed['gpu_used'] is True
    assert observed['technologies']==['GPU comparison','Vulkan']
    assert runtime_observation(None,{'execution_backend':'cpu','method':'comparison'},'compai.local')['gpu_used'] is False
    assert runtime_observation(None,{'gpu_qualification':'qualified'},'compai.local')['gpu_used'] is False
    metadata=gpu_execution_metadata({'gpu':{'uuid':'GPU-device','runtime_directory':'private-runtime'}})
    assert metadata=={'gpu':True,'gpu_devices':['GPU-device']}
    assert runtime_observation(metadata,{},'compai.local')['gpu_devices']==['GPU-device']


def test_completed_gpu_comparison_keeps_submission_estimate_separate(tmp_path):
    from submission_queue import SubmissionQueue
    queue=SubmissionQueue('comparison',tmp_path/'queue')
    queue.submit({'id':'compare','resources':{'cpus':2,'memory_mb':1280,'sink':'cpu'}})
    claim=queue.claim('compai.local')
    queue.update('compare',claim['token'],state='completed',result=json.dumps({'execution_backend':'vulkan','method':'comparison'}))
    item=snapshot(tmp_path/'queue')['items'][0]
    assert item['resources']['sink']=='gpu'
    assert item['submission_resources']['sink']=='cpu'
    assert item['runtime']['gpu_used'] is True


def test_blocked_followup_explains_qualification_and_evidence_gates():
    from task_pipeline import blocked_detail
    assert 'qualification' in blocked_detail({},'waiting_implementation')
    assert 'venter-final-publication' in blocked_detail({'kind':'adopted','required_gates':['venter-final-publication']},'waiting_dependency')
    assert blocked_detail({},'running') is None


def test_adopted_finalizer_reports_real_storage_blocker(tmp_path):
    from task_pipeline import finalizer_blocker
    import json
    folder=tmp_path/'recall/venter/finalize';folder.mkdir(parents=True)
    (folder/'state.json').write_text(json.dumps({'phase':'waiting_for_sufficient_ssd_scratch','storage_plan':{'conservative_peak_additional_bytes':{'latitude':404000000000},'candidates':{'latitude':228000000000}}}))
    spec={'kind':'adopted','executor':{'unit':'lifetech-wgs-venter-finalize.service'}}
    assert '404.0 GB additional, 228.0 GB available' in finalizer_blocker(spec,tmp_path)
    assert finalizer_blocker({},tmp_path) is None


def test_followup_timing_and_title_use_exact_worker_attempt(tmp_path):
    from submission_queue import SubmissionQueue
    ns='genomics-followup:revision';ident='call-julian-GRCh38-1-0-5000000'
    queue=SubmissionQueue(ns,tmp_path/'queue')
    queue.submit({'id':ident,'kind':'regional','sample':'julian','reference_space':'GRCh38','chromosome':'1',
                  'region':{'start':0,'end':5000000},'resources':{'cpus':2,'memory_mb':1280}})
    claim=queue.claim('worker')
    queue.update(ident,claim['token'],state='accepted',request=json.dumps({'directory':'/attempt/current','image':'tools','binding':'recipe','admission_pending':False}))
    report={'host':'worker','container':'worker-current','phase':'running','attempt_directory':'/attempt/current',
            'started':'2026-11-04T12:00:00+00:00','cpus':2,'memory_mb':32000,'memory_reservation_mb':1280,
            'sink':'cpu','image':'tools','profiling_context':{'recipe_sha256':'recipe'},'recorded_at':1793793600,'command':'PRIVATE COMMAND'}
    with sqlite3.connect(tmp_path/'runtime') as db:
        db.execute('CREATE TABLE observations(job,container,report)')
        db.execute('INSERT INTO observations VALUES(?,?,?)',(ns+'#'+ident,report['container'],json.dumps(report)))
    row=snapshot(tmp_path/'queue',tmp_path,runtime_path=tmp_path/'runtime')['items'][0]
    assert row['status']=='running'
    assert row['name']=='Variant calling · Julian · GRCh38 · chr1:0–5,000,000 (0-based)'
    assert row['runtime']['started']==report['started'] and row['runtime']['elapsed_seconds']>0
    assert 'queue waiting time excluded' in row['runtime']['timing_basis']
    assert row['worker_resources']['memory_mb']==1280
    assert 'PRIVATE COMMAND' not in json.dumps(row)
    with sqlite3.connect(tmp_path/'queue') as db:
        db.execute('UPDATE submissions SET request=?',(json.dumps({'directory':'/attempt/new'}),))
    row=snapshot(tmp_path/'queue',tmp_path,runtime_path=tmp_path/'runtime')['items'][0]
    assert row['status']=='preparing' and row['runtime']['started'] is None
    with sqlite3.connect(tmp_path/'queue') as db:
        db.execute("UPDATE submissions SET state='completed',request=?",(json.dumps({'directory':'/attempt/current','image':'tools','binding':'recipe'}),))
    row=snapshot(tmp_path/'queue',tmp_path,runtime_path=tmp_path/'runtime')['items'][0]
    assert row['status']=='completed' and row['runtime']['started'] is None
    report.update(phase='complete',finished='2026-11-04T12:02:00+00:00',elapsed_seconds=120)
    with sqlite3.connect(tmp_path/'runtime') as db:
        db.execute('UPDATE observations SET report=?',(json.dumps(report),))
    row=snapshot(tmp_path/'queue',tmp_path,runtime_path=tmp_path/'runtime')['items'][0]
    assert row['status']=='completed' and row['runtime']['elapsed_seconds']==120


def test_followup_completed_time_stays_fixed_and_old_running_sample_is_ignored(tmp_path):
    from task_pipeline import runtime_observation
    report={'started':'2026-11-04T12:00:00+00:00','finished':'2026-11-04T12:02:00+00:00'}
    assert runtime_observation(report,{},'worker')['elapsed_seconds']==120
    from task_pipeline import shard_title
    assert shard_title({'kind':'qualification'},'qualify-calling','genomics-followup:x')=='Executor controls · Calling'
    assert shard_title({},'legacy-job','legacy')=='legacy-job'


def test_followup_work_has_its_own_run_type():
    from task_pipeline import run_type
    assert run_type({'kind':'regional'}, {},'genomics-followup:revision')=='genome-analysis'
    assert run_type({'kind':'regional'}, {},'unrelated')=='other'


def test_finalizer_workers_have_machine_timing_and_do_not_duplicate_forecast(tmp_path):
    import time
    from task_pipeline import project_finalizer,runtime_summary
    gen='a'*12;run='SRR7097858-'+gen+'-000007'
    folder=tmp_path/'recall/venter/finalize';folder.mkdir(parents=True)
    (folder/'state.json').write_text(json.dumps({'generation':gen}))
    runtime=tmp_path/'runtime'
    with sqlite3.connect(runtime) as db:
        db.execute('CREATE TABLE finalizer_observations(generation,id,report)')
        for ident,row in [('__workflow__',{'host':'latitude','phase':'bounded_mate_preparation','prepared':0,'total':1,'sampled_at':time.time()}),
                          (run,{'host':'latitude','container':'worker','phase':'running','status':'running','started':'2026-11-04T12:00:00+00:00','cpus':2,'memory_mb':32000,'memory_reservation_mb':1024,'sampled_at':time.time()})]:
            db.execute('INSERT INTO finalizer_observations VALUES(?,?,?)',(gen,ident,json.dumps(row)))
    items=[{'id':'parent/adopt-venter-finalizer','job_id':'adopt-venter-finalizer','parent':'parent','status':'running'}]
    project_finalizer(items,tmp_path,runtime)
    assert len(items)==2 and items[0]['worker']=='latitude' and items[0]['status']=='waiting_dependency'
    child=items[1];assert child['status']=='running' and child['worker']=='latitude' and child['runtime']['started']
    assert child['worker_resources']['memory_mb']==1024
    assert runtime_summary(items)['remaining_jobs']==1
    child.update(queue_state='accepted',parent='venter-finalization:'+gen)
    project_finalizer(items,tmp_path,runtime)
    assert len(items)==2 and child['parent']=='parent' and child['queue_state']=='accepted'
    assert child['runtime']['started'] and child['worker']=='latitude'


def test_terminal_queue_failure_cannot_be_hidden_as_retrying(tmp_path):
    from submission_queue import SubmissionQueue
    ns='genomics-followup:rev';ident='call-test';queue=SubmissionQueue(ns,tmp_path/'q')
    queue.submit({'id':ident});claim=queue.claim('node')
    queue.update(ident,claim['token'],state='failed',request=json.dumps({'directory':'attempt','image':'image','binding':'recipe'}))
    report={'container':'worker','host':'node','phase':'failed','image':'image','attempt_directory':'attempt','finished':'2026-11-04T12:00:00+00:00','profiling_context':{'recipe_sha256':'recipe'}}
    with sqlite3.connect(tmp_path/'runtime') as db:
        db.execute('CREATE TABLE observations(job,container,report)');db.execute('INSERT INTO observations VALUES(?,?,?)',(ns+'#'+ident,'worker',json.dumps(report)))
    assert snapshot(tmp_path/'q',tmp_path,tmp_path/'runtime')['items'][0]['status']=='failed'
def test_completed_regional_preparation_uses_its_retained_extraction_attempt(tmp_path):
    from submission_queue import SubmissionQueue
    from task_pipeline import snapshot
    ns='genomics-followup-inputs:rev';q=SubmissionQueue(ns,tmp_path/'q')
    q.submit({'id':'region-one','resources':{'cpus':1,'memory_mb':1024}})
    claim=q.claim('latitude')
    q.update('region-one',claim['token'],state='completed',request=json.dumps({'directory':'/attempt','source_image':'tools'}),result='{}')
    report={'host':'latitude','container':'extractor','image':'tools','phase':'complete','attempt_directory':'/attempt',
            'started':'2026-11-05T12:00:00+00:00','finished':'2026-11-05T12:01:25+00:00','cpus':1,'memory_reservation_mb':1024,
            'cpu_core_seconds':24,'memory_peak_bytes':456}
    with sqlite3.connect(tmp_path/'runtime') as db:
        db.execute('CREATE TABLE observations(job,container,report)')
        db.execute('INSERT INTO observations VALUES(?,?,?)',(ns+'#region-one','extractor',json.dumps(report)))
    item=snapshot(tmp_path/'q',tmp_path,tmp_path/'runtime')['items'][0]
    assert item['runtime']['elapsed_seconds']==85 and item['runtime']['cpu_core_seconds']==24
    assert item['execution']['memory_peak_bytes']==456
    with sqlite3.connect(tmp_path/'q') as db:db.execute('UPDATE submissions SET request=?',(json.dumps({'directory':'/different-attempt','source_image':'tools'}),))
    assert snapshot(tmp_path/'q',tmp_path,tmp_path/'runtime')['items'][0]['runtime']['started'] is None


def test_gpu_smoke_attempts_collapse_only_with_identical_scientific_specification():
    from task_pipeline import collapse_test_attempts
    base={'kind':'regional','job_id':'compare-region','validation_identity':'a'*64,'status':'failed','queue_state':'failed','worker':'intel','detail':'missing job.json','created':1,'updated':2,'id':'old/compare-region','parent':'intel-routing-smoke:old'}
    newer={**base,'id':'new/compare-region','parent':'intel-routing-smoke:new','status':'completed','queue_state':'completed','created':3,'updated':4,'detail':None}
    distinct={**newer,'id':'different/compare-region','parent':'intel-routing-smoke:different','validation_identity':'b'*64}
    rows=collapse_test_attempts([base,newer,distinct])
    assert len(rows)==2
    recovered=next(r for r in rows if r['validation_identity']=='a'*64)
    assert recovered['status']=='completed' and recovered['attempt_id']=='new/compare-region'
    assert [a['status'] for a in recovered['attempts']]==['completed','failed']
    assert recovered['attempts'][1]['detail']=='missing job.json'
