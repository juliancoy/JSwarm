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
