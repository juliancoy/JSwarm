from unittest.mock import Mock,patch
import pipeline_nodes as pipeline
from node_pipeline_api import report_key


def item():
    return {'id':'workflow/task','parent':'workflow','job_id':'task','task_id':None,'worker':'node.local',
            'queue_node':'master.local','queue_state':'accepted','status':'preparing','resources':{'cpus':1},
            'report_keys':[report_key('/owned/execution.json')]}


def test_execution_is_from_matching_owner_and_staleness_is_explicit():
    row={'job':'a'*32,'controller_report':'/owned/execution.json','running':True,'container':'worker',
         'cpu_core_seconds':45,'cpus':2,'memory_mb':100,'observed_at':100}
    value=pipeline.merge_item(item(),{'status':'live','observed_at':101,'records':{'a'*32:row}})
    assert value['status']=='running' and value['runtime']['cpu_core_seconds']==45
    assert value['worker_resources']['cpus']==2
    stale=pipeline.merge_item(item(),{'status':'unavailable','records':{'a'*32:row}})
    assert stale['status']=='unknown' and 'stale' in stale['detail']
    foreign={**row,'controller_report':'/different/attempt/execution.json'}
    assert not pipeline.merge_item(item(),{'status':'live','records':{'a'*32:foreign}}).get('execution')


def test_worker_exit_does_not_claim_verified_completion():
    row={'controller_report':'/owned/execution.json','running':False,'exit_code':0,'observed_at':100}
    assert pipeline.merge_item(item(),{'status':'live','records':{'job':row}})['status']=='preparing'
    accepted={**item(),'queue_state':'completed','status':'completed'}
    assert pipeline.merge_item(accepted,{'status':'unavailable'})['status']=='completed'


def test_stream_timings_are_bound_to_node_task_identity():
    source={**item(),'task_id':'expected'}
    node={'status':'live','streams':{'other':{'phase':'running','cpu_core_seconds':999}}}
    assert not pipeline.merge_item(source,node).get('execution')
    node['streams']['expected']={'state':'running','cpu_core_seconds':12,'resources':{'cpus':3}}
    result=pipeline.merge_item(source,node)
    assert result['status']=='running' and result['runtime']['cpu_core_seconds']==12


def test_collector_uses_encrypted_gateway_and_no_coordinator_telemetry():
    client=Mock()
    client.call.side_effect=[{'node':'node.local','workers':[]},{'items':[{'job':'a'*32}],'next':None},{'items':{'task':{'state':'running'}}}]
    with patch.object(pipeline,'AgentClient',return_value=client) as create:
        pipeline.CACHE['node.local']={}
        pipeline.collect('node.local','192.0.2.1',{},[('task',False)],wan_only=True)
    assert create.call_args.args[0]['agent_route']=='webrtc'
    assert [call.args[0] for call in client.call.call_args_list]==['status','worker.records','stream.timings']
    assert pipeline.CACHE['node.local']['value']['status']=='live'


def test_verified_gpu_receipt_survives_node_merge_and_allocation_is_not_kernel_proof():
    row={'controller_report':'/owned/execution.json','running':False,'exit_code':0,'observed_at':100,'sink':'gpu','gpu_allocation':'GPU-one'}
    source={**item(),'queue_state':'completed','status':'completed','runtime':{'gpu_used':True,'gpu_devices':['GPU-one'],'technologies':['GPU comparison','Vulkan']}}
    value=pipeline.merge_item(source,{'status':'live','records':{'job':row}})
    assert value['runtime']['gpu_used'] and value['runtime']['gpu_devices']==['GPU-one']
    assert 'Vulkan' in value['runtime']['technologies']
    unverified=pipeline.merge_item(item(),{'status':'live','records':{'job':row}})
    assert not unverified['runtime']['gpu_used'] and unverified['runtime']['gpu_devices']==['GPU-one']


def test_batch_members_join_shared_stream_and_keep_scope():
    source={**item(),'task_id':'batch-one','shared_execution':{'id':'batch-one','members':20}}
    value=pipeline.merge_item(source,{'status':'live','streams':{'batch-one':{'state':'running','cpu_core_seconds':12,'resources':{'cpus':2}}}})
    assert value['status']=='running' and 'do not sum' in value['runtime']['accounting_scope']


def test_queue_projects_verified_gpu_receipt_and_batch_identity_without_paths(tmp_path):
    import json
    from submission_queue import SubmissionQueue
    from node_pipeline_api import queue_snapshot
    q=SubmissionQueue('workflow',tmp_path/'queue');q.submit_many([{'id':'shard'}])
    request={'gpu_batch':{'id':'shared-batch','manifest':'/private/manifest','members':['shard','other']},'accelerator':{'gpu':{'uuid':'GPU-one'}}}
    receipt={'receipt_sha256':'a'*64,'execution_backend':'vulkan','method':'comparison','directory':'/private/results'}
    with q.connect() as db:db.execute('UPDATE submissions SET state=?,host=?,request=?,result=?',('completed','node.local',json.dumps(request),json.dumps(receipt)))
    row=queue_snapshot(q.path,'master')['items'][0]
    assert row['task_id']=='shared-batch' and row['runtime']['gpu_used'] and row['runtime']['gpu_devices']==['GPU-one']
    assert row['shared_execution']['members']==2 and '/private' not in json.dumps(row)
    with q.connect() as db:db.execute('UPDATE submissions SET result=?',(json.dumps({**receipt,'receipt_sha256':None}),))
    assert not queue_snapshot(q.path,'master')['items'][0]['runtime']


def test_unqueued_node_work_is_visible_without_duplicate_queue_jobs_or_cache_holders():
    queued=item()
    workers=[{'job':'queued','group':'epigenetic-full','active':True,'container':'worker-one','cpus':6,'memory_mb':16384},
             {'job':'standalone','group':'gpu-qc-service','active':True,'container':'worker-two','cpus':1,'memory_mb':512,'sink':'gpu'},
             {'job':'cache','group':'shared-cache','active':True},
             {'job':'stopped','group':'old-work','active':False}]
    node={'status':'live','observed_at':101,'active_workers':workers,'records':{
        'queued':{'controller_report':'/owned/execution.json'},
        'standalone':{'running':True,'cpus':1,'memory_mb':512,'started_at':'2026-11-15T20:00:00Z'}}}
    rows=pipeline.node_workloads([queued],{'node.local':node})
    assert len(rows)==1
    assert rows[0]['id']=='node-worker/node.local/standalone'
    assert rows[0]['status']=='running' and rows[0]['worker_resources']['memory_mb']==512
    assert rows[0]['data_sources']=={'queue':None,'execution':'node.local'}
    assert '/owned' not in str(rows)
    assert not pipeline.node_workloads([queued],{'node.local':{**node,'status':'stale'}})
    assert not pipeline.node_workloads([queued],{'node.local':{**node,'status':'unavailable'}})


def test_node_work_deduplication_uses_owner_and_attempt_identity():
    source={**item(),'task_id':'standalone'}
    worker={'job':'standalone','group':'other','active':True,'cpus':1}
    node={'status':'live','observed_at':101,'active_workers':[worker],'records':{}}
    assert not pipeline.node_workloads([source],{'node.local':node})
    assert len(pipeline.node_workloads([source],{'different.local':node}))==1


def test_cache_holders_do_not_exhaust_active_compute_refresh_budget():
    client=Mock()
    compute={'group':'epigenetic-full','job':'a'*32,'container':'swarm-job-'+'a'*32,'active':True}
    client.call.side_effect=[{'node':'node.local','workers':[{'group':'shared-cache','active':True}]*110+[compute]},
                             {'items':[],'next':None},{'found':True,'job':'a'*32,'running':True}]
    with patch.object(pipeline,'AgentClient',return_value=client):
        pipeline.CACHE['node.local']={}
        pipeline.collect('node.local','192.0.2.1',{},[])
    assert [call.args[0] for call in client.call.call_args_list]==['status','worker.records','worker.status']
    assert pipeline.CACHE['node.local']['value']['active_workers']==[compute]
