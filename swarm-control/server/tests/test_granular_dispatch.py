import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from distributed_worker import PYTHON, SUITES
from granular_dispatch import execute


class DispatchLifecycleTests(unittest.TestCase):
    def test_completed_session_drains_events_without_feeding_it_again(self):
        self.dispatch()

    def test_python_jobs_finish_before_cold_node_image_preparation(self):
        self.dispatch(cold_image=True)

    def test_cold_first_worker_does_not_pin_a_single_shard(self):
        self.dispatch(slow_peer=True)

    def dispatch(self, cold_image=False, slow_peer=False):
        task='monitor--abc'
        tasks=[task,'map--abc'] if cold_image else [task]
        events=[]
        slow_started=threading.Event();fast_finished=threading.Event()
        state={'status':'running','closes':0}
        host={'host':'worker','session':{'machine':'machine'},'agent_status':{'source_revision':'source'}}
        def call(operation,args=None,**kwargs):
            if operation=='worker.policy':return {'accepting_jobs':True,'reason':'Accepting worker jobs'}
            if operation=='status':return {'ready':True,'machine':'machine','source_revision':'source'}
            if operation=='job.start':
                state['status']='running';events.clear()
                events.append({'kind':'capacity','cpus':4,'memory_mb':4096})
                for base in args['manifest']['hosts'][args['index']]['suites']:
                    events.append({'kind':'dependencies','base':base,'status':'ready'})
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
                    fast_finished.set()
                return {'accepted':args['tasks']}
            raise AssertionError(operation)
        with tempfile.TemporaryDirectory() as folder:
            run={'id':'20261024T000000Z-1234abcd','hosts':[host],'images':{},'source_sha256':'digest','force':True,
                 'selection':{'paths':[],'suites':['monitor','map'] if cold_image else ['monitor']},
                 'repositories':{name:{'url':'url','commit':'commit'} for name in ('.','OrgPortal','PIdP')}}
            service=SimpleNamespace(run=run,root=folder,folder=folder,lock=threading.RLock(),save=Mock())
            def update(worker,event,**kwargs):
                if event.get('suite') in worker['jobs']:worker['jobs'][event['suite']].update(status=event['status'])
            service.update=update
            service.update_events=lambda worker,events:[update(worker,event) for event in events]
            images=Mock(host_images={'worker':{PYTHON:'image'}},images={PYTHON:'image'})
            spec={**SUITES['monitor'],'base':'monitor','cpus':1,'memory_mb':512}
            specs={task:spec}
            if cold_image:
                specs['map--abc']={**SUITES['map'],'base':'map','cpus':1,'memory_mb':512}
                def ensure(image,hosts):
                    if image==SUITES['map']['image']:
                        self.assertEqual(host['jobs'][task]['status'],'passed')
                        images.host_images['worker'][image]='node-image'
                images.ensure.side_effect=ensure
            if slow_peer:
                def ensure(image,hosts):
                    if hosts[0]['host']=='cold-worker':
                        slow_started.set()
                        assert fast_finished.wait(5), 'Cold preparation blocked the ready worker'
                    else:
                        assert slow_started.wait(5), 'Cold worker was excluded before preparation'
                images.ensure.side_effect=ensure
            with patch.dict('os.environ' ,{'SWARM_SUBMISSION_QUEUE':folder+'/submission.sqlite'}), patch.dict(SUITES,specs), patch('granular_dispatch.catalog',return_value=tasks), \
                 patch('granular_dispatch.pending',side_effect=lambda *_:list(tasks)), patch('granular_dispatch.SuiteCache'), \
                 patch('granular_dispatch.AgentClient') as client, patch('granular_dispatch.time.sleep'):
                client.return_value.call.side_effect=call
                cold={'host':'cold-worker'}
                execute(service,[cold,host] if slow_peer else [host],images)
                if slow_peer:
                    self.assertTrue(cold['not_executed'])
                    self.assertEqual(cold['jobs'],{})
                self.assertEqual(client.call_count,len(tasks)+(1 if slow_peer else 0))
            self.assertEqual(host['status'],'passed')
            self.assertEqual(host['jobs'][task]['status'],'passed')
            self.assertEqual(state['closes'],len(tasks))
            self.assertEqual(run['pending_tasks'],[])


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
