import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from distributed_worker import PYTHON, SUITES
from granular_dispatch import execute


class DispatchLifecycleTests(unittest.TestCase):
    def test_completed_session_drains_events_without_feeding_it_again(self):
        task='monitor--abc'
        events=[]
        state={'status':'running','closes':0}
        host={'host':'worker','session':{'machine':'machine'},'agent_status':{'source_revision':'source'}}
        def call(operation,args=None,**kwargs):
            if operation=='worker.policy':return {'accepting_jobs':True,'reason':'Accepting worker jobs'}
            if operation=='status':return {'ready':True,'machine':'machine','source_revision':'source'}
            if operation=='job.start':
                events.append({'kind':'capacity','cpus':4,'memory_mb':4096})
                events.append({'kind':'dependencies','base':'monitor','status':'ready'})
                events.append({'kind':'dependencies','status':'complete'})
                return {'status':'running'}
            if operation=='job.poll':
                cursor=args['cursor']
                return {'status':state['status'],'cursor':len(events),
                        'events':[{'sequence':i+1,'event':event} for i,event in enumerate(events) if i+1>cursor]}
            if operation=='job.feed':
                if state['status']!='running':raise RuntimeError('Already completed')
                for ident in args['tasks']:
                    events.append({'kind':'suite','suite':ident,'status':'passed','duration_seconds':1})
                if args.get('close'):
                    state['closes']+=1
                    events.append({'kind':'complete'})
                    state['status']='completed'
                return {'accepted':args['tasks']}
            raise AssertionError(operation)
        with tempfile.TemporaryDirectory() as folder:
            run={'id':'20261024T000000Z-1234abcd','hosts':[host],'images':{},'source_sha256':'digest','force':True,
                 'selection':{'paths':[],'suites':['monitor']},
                 'repositories':{name:{'url':'url','commit':'commit'} for name in ('.','OrgPortal','PIdP')}}
            service=SimpleNamespace(run=run,root=folder,folder=folder,lock=threading.RLock(),save=Mock())
            def update(worker,event,**kwargs):
                if event.get('suite') in worker['jobs']:worker['jobs'][event['suite']].update(status=event['status'])
            service.update=update
            service.update_events=lambda worker,events:[update(worker,event) for event in events]
            images=Mock(host_images={'worker':{PYTHON:'image'}},images={PYTHON:'image'})
            spec={**SUITES['monitor'],'base':'monitor','cpus':1,'memory_mb':512}
            with patch.dict('os.environ',{'SWARM_SUBMISSION_QUEUE':folder+'/submission.sqlite'}), patch.dict(SUITES,{task:spec}), patch('granular_dispatch.catalog',return_value=[task]), \
                 patch('granular_dispatch.pending',return_value=[task]), patch('granular_dispatch.SuiteCache'), \
                 patch('granular_dispatch.focused_workers',side_effect=lambda hosts,*args:hosts[:1]), \
                 patch('granular_dispatch.AgentClient') as client, patch('granular_dispatch.time.sleep'):
                client.return_value.call.side_effect=call
                unused={'host':'unused-worker'}
                execute(service,[host,unused],images)
                self.assertTrue(unused['not_executed'])
                self.assertEqual(client.call_count,1)
            self.assertEqual(host['status'],'passed')
            self.assertEqual(host['jobs'][task]['status'],'passed')
            self.assertEqual(state['closes'],1)
            self.assertEqual(run['pending_tasks'],[])


def test_focused_worker_selection_skips_full_node_and_prefers_cached_images():
    from granular_dispatch import focused_workers
    hosts=[{'host':n} for n in ('full','cold','cached')]
    def client(host):
        instance=Mock()
        def call(op,args=None,**kwargs):
            if op=='resource.capacity':return {'available_mb':500 if host['host']=='full' else 5000,'available_cpus':4}
            if op=='image.inspect':return {'id':'cached'} if host['host']=='cached' else None
            raise AssertionError(op)
        instance.call.side_effect=call;return instance
    with patch('granular_dispatch.AgentClient',side_effect=client):
        assert focused_workers(hosts,['test'],{'test':{'image':'tools'}})==[hosts[2]]


def test_focused_worker_selection_keeps_fallbacks_when_every_node_is_full():
    from granular_dispatch import focused_workers
    hosts=[{'host':'one'},{'host':'two'}]
    with patch('granular_dispatch.AgentClient') as client:
        client.return_value.call.return_value={'available_mb':0,'available_cpus':0}
        assert focused_workers(hosts,['test'],{'test':{'image':'tools'}})==hosts


def test_transport_recovery_keeps_same_operation_and_reports_reconnection(monkeypatch):
    import granular_dispatch as dispatch
    calls=[];states=[];accepted=set()
    payload={'id':'same-run','tasks':['same-task'],'close':False}
    def operation():
        calls.append(dict(payload));accepted.update(payload['tasks'])
        if len(calls)==1:raise TimeoutError('lost acknowledgement')
        return {'accepted':list(accepted)}
    monkeypatch.setattr(dispatch.time,'sleep',lambda _:None)
    result=dispatch.recover_transport(operation,lambda:False,dispatch.time.monotonic()+10,states.append)
    assert result=={'accepted':['same-task']} and calls==[payload,payload]
    assert states==[True,False] and len(accepted)==1


def test_transport_recovery_does_not_retry_identity_or_worker_errors():
    import granular_dispatch as dispatch
    import pytest
    for error in (RuntimeError('identity mismatch'),PermissionError('key denied'),FileNotFoundError('key removed')):
        with pytest.raises(type(error)):
            dispatch.recover_transport(lambda:(_ for _ in ()).throw(error),lambda:False,dispatch.time.monotonic()+10,lambda _:None)
