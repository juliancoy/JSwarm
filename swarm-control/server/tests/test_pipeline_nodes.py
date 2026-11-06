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
