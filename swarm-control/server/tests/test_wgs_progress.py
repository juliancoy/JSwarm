import json
from wgs_progress import snapshot


def test_progress_projects_only_operational_counts(tmp_path):
    folder=tmp_path/'downloads';folder.mkdir()
    (folder/'state.json').write_text(json.dumps({'status':'running','workers':[{'host':'peer','cache':'/secret/path','free_bytes':500}], 'jobs':{'a':{'host':'peer','sample':'Watson','bytes':100,'status':'verified','receipt':{'path':'/private/reads','url':'private'}},'b':{'host':'peer','sample':'Watson','bytes':100,'status':'downloading','progress':{'bytes_completed':40}}}}))
    result=snapshot(tmp_path);sample=result['samples'][0]
    assert sample['files_verified']==1 and sample['files_active']==1 and sample['files_failed']==0
    assert sample['bytes_received']==140 and sample['bytes_total']==200
    assert result['storage']==[{'host':'peer','free_bytes_at_start':500,'assigned_bytes':200}]
    assert 'secret' not in str(result) and 'private' not in str(result)


def test_absent_or_broken_state_is_unavailable(tmp_path):
    assert not snapshot(tmp_path)['available']
    folder=tmp_path/'downloads';folder.mkdir();(folder/'state.json').write_text('partial')
    assert not snapshot(tmp_path)['available']


def test_gpu_qc_exposes_only_aggregate_progress(tmp_path):
    (tmp_path/'gpu-qc').mkdir();(tmp_path/'qc').mkdir()
    (tmp_path/'gpu-qc/service.json').write_text(json.dumps({'status':'running','host':'peer','batches':4,'bases':200,'socket':'/secret/socket','raw_sequence':'private'}))
    (tmp_path/'qc/state.json').write_text(json.dumps({'jobs':{'a':{'gpu_batches':3,'gpu_bases':150,'cpu_bases':20,'quality_histogram':[1]*94}}}))
    value=snapshot(tmp_path)
    assert value['gpu_qc']['batches']==4
    assert value['qc']['gpu_batches']==3 and value['qc']['gpu_bases']==150 and value['qc']['cpu_bases']==20
    assert 'secret' not in str(value) and 'private' not in str(value) and 'quality_histogram' not in str(value)


def test_backfill_is_reported_separately_from_source_qc(tmp_path):
    path=tmp_path/'qc/gpu-replay';path.mkdir(parents=True)
    (path/'state.json').write_text(json.dumps({'status':'running','jobs':{'a':{'phase':'verified','provisional':False,'gpu_batches':3,'gpu_bases':42,'gpu_cpu_crosschecked_batches':2,'quality_histogram':[0]*94}}}))
    value=snapshot(tmp_path)
    assert value['qc']['reads_processed']==0 and value['qc']['gpu_bases']==0
    assert value['gpu_backfill']=={'status':'running','files':1,'verified_files':1,'batches':3,'bases':42,'cpu_crosschecked_batches':2}
    (path/'state.json').write_text(json.dumps({'jobs':{'a':{'phase':'verified','status':'waiting_gpu','provisional':True,'gpu_bases':20,'cpu_bases':22}}}))
    assert snapshot(tmp_path)['gpu_backfill']['verified_files']==0


def test_gpu_workers_count_only_verified_files_and_keep_validation_distinct(tmp_path):
    path=tmp_path/'gpu-qc/compai-backfill';path.mkdir(parents=True)
    (path/'state.json').write_text(json.dumps({'jobs':{
        'a':{'status':'verified','provisional':False,'gpu_bases':42,'matches_original_cpu_qc':True,'gpu_cpu_crosschecked_batches':0,'sequence':'private'},
        'b':{'status':'running','provisional':True,'gpu_bases':100}}}))
    (tmp_path/'gpu-qc/daemon.json').write_text(json.dumps({'device':'RTX 3060','startup_fixtures':4,'socket':'/secret/socket','bases':100000}))
    value=snapshot(tmp_path);worker=value['gpu_workers'][0]
    assert worker['verified_files']==1 and worker['bases']==42
    assert worker['matches_original_cpu_qc']==1 and worker['cpu_crosschecked_batches']==0
    assert value['qc']['bases_processed']==0
    assert not value['gpu_performance']['available']
    assert 'private' not in str(value) and 'secret' not in str(value)


def test_primary_venter_gpu_work_is_visible_separately_from_verified_backfill(tmp_path):
    (tmp_path/'qc').mkdir();(tmp_path/'gpu-qc').mkdir()
    (tmp_path/'qc/state.json').write_text(json.dumps({'jobs':{
        'first':{'sample':'Venter','host':'compai.local','status':'streaming','phase':'streaming','provisional':True,'gpu_bases':123,'gpu_batches':4,'gpu_cpu_crosschecked_batches':1,'gpu_fallback_batches':0},
        'second':{'sample':'Venter','host':'julian-latitude.local','status':'waiting_capacity','provisional':True}}}))
    (tmp_path/'gpu-qc/latitude-service.json').write_text(json.dumps({'status':'running','device':'Intel GPU'}))
    value=snapshot(tmp_path);workers={row['host']:row for row in value['gpu_workers']}
    assert workers['compai.local']['status']=='streaming'
    assert workers['compai.local']['streaming_bases']==123 and workers['compai.local']['streaming_batches']==4
    assert workers['compai.local']['streaming_cpu_crosschecked_batches']==1 and workers['compai.local']['verified_files']==0
    assert workers['julian-latitude.local']['status']=='waiting_capacity' and workers['julian-latitude.local']['device']=='Intel GPU'


def test_fresh_reconciled_receipt_repairs_download_progress_without_restarting_transfer(tmp_path):
    import hashlib
    item={'url':'https://archive.example/public.gz','filename':'public.gz','bytes':100,'md5':'a'*32,'read_count':20,'sample':'Watson','run':'public'}
    ident=hashlib.sha256(item['url'].encode()).hexdigest()[:20]
    (tmp_path/'downloads').mkdir()
    (tmp_path/'catalog.json').write_text(json.dumps({'files':[item]}))
    (tmp_path/'downloads/state.json').write_text(json.dumps({'status':'running','jobs':{ident:{**item,'host':'peer','status':'failed'}}}))
    (tmp_path/'downloads/reconciled-receipts.json').write_text(json.dumps({'receipts':{ident:{**item,'verification':'publisher_md5_rechecked','sha256':'c'*64,'verified_at':100}}}))
    sample=snapshot(tmp_path)['samples'][0]
    assert sample['files_verified']==1 and sample['files_failed']==0 and sample['bytes_verified']==100
