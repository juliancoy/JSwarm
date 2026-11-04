import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from distributed import DistributedTests, plan, targets, no_remote_shell

class DistributedTestsTests(unittest.TestCase):
    def test_focused_plan_contains_no_placeholder_p2p_job(self):
        hosts=plan([{'host':'node'}],{'paths':['tests/one.py'],'suites':[]})
        self.assertEqual(hosts[0]['jobs'],{})
        self.assertEqual(hosts[0]['suites'],[])

    def test_stop_is_idempotent_and_targets_only_current_run(self):
        with tempfile.TemporaryDirectory() as folder:
            service = DistributedTests(state=folder)
            self.assertFalse(service.stop())
            service.running = True
            service.run = {'id': '20261023T120000Z-1234abcd', 'status': 'running',
                           'hosts': plan([{'host': 'a.local', 'user': 'julian'}])}
            with patch('distributed.threading.Thread') as thread:
                self.assertTrue(service.stop())
                self.assertFalse(service.stop())
                thread.return_value.start.assert_called_once()
            self.assertEqual(service.view()['run']['status'], 'stopping')
            service.run['hosts'][0]['agent_endpoint']='http://10.42.0.2:8790'
            with patch('agent_client.AgentClient') as client:
                client.return_value.call.return_value={'acknowledged':True}
                service.stop_hosts(service.run)
                client.return_value.call.assert_called_once_with('job.stop',{'id':service.run['id']},timeout=40)
                self.assertTrue(service.run['hosts'][0]['stop_acknowledged'])

    def test_received_batch_is_journaled_before_one_materialized_checkpoint(self):
        from task_events import EventStore
        with tempfile.TemporaryDirectory() as folder:
            service=DistributedTests(state=folder)
            host={'host':'worker','jobs':{'check':{'status':'running','logs':[]}}}
            service.run={'id':'batch-run','hosts':[host]};service.last_save=0
            events=[{'kind':'log','suite':'check','line':'ready','_worker_sequence':1,'_worker_job':'job'},
                    {'kind':'suite','suite':'check','status':'passed','_worker_sequence':2,'_worker_job':'job'}]
            with patch.object(service,'save') as checkpoint:
                service.update_events(host,events)
                checkpoint.assert_not_called()
            self.assertEqual(host['jobs']['check']['status'],'passed')
            self.assertEqual(host['jobs']['check']['logs'],['ready'])
            store=EventStore(Path(folder)/'compute-events.sqlite3')
            self.assertEqual(store.history('batch-run')['total_events'],2)
            service.save()
            self.assertEqual(store.history('batch-run')['total_events'],3)

    def test_inventory_uses_enrollment_and_never_falls_back_to_ssh(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'peers.json'
            path.write_text(json.dumps({'name':'a.local','peers':{'a.local':'10.42.0.1','b.local':'10.42.0.2'}}))
            with patch('agent_client.CONFIG',path):workers=targets()
            self.assertEqual([h['host'] for h in workers],['a.local','b.local'])
            self.assertTrue(workers[0]['local'])
            with self.assertRaises(RuntimeError):no_remote_shell(workers[1])

    def test_machines_do_not_own_application_suites(self):
        hosts = plan([{'host': name + '.local'} for name in ['a', 'b', 'c']])
        for host in hosts:
            self.assertEqual(host['suites'], ['p2p'])
            self.assertEqual(set(host['jobs']), {'p2p'})
            self.assertNotIn('org_api_shard', host)

    def test_readiness_excludes_offline_workers_before_queue_dispatch(self):
        with tempfile.TemporaryDirectory() as folder:
            service=DistributedTests(state=folder)
            original=plan([{'host':name+'.local'} for name in ('a','b','offline')])
            service.start(selected_hosts=original,launch=False)
            with patch('agent_client.AgentSessions') as sessions, patch('distributed.ImageDistributor') as images, patch('github_sources.publish',side_effect=RuntimeError('stop after planning')):
                images.return_value.failures={}
                sessions.return_value.accessible.return_value=original
                sessions.return_value.verify.return_value=original[:2]
                service.execute()
            active=[h for h in service.run['hosts'] if not h.get('excluded')]
            self.assertTrue(all(h['suites']==['p2p'] for h in active))
            offline=next(h for h in service.run['hosts'] if h.get('excluded'))
            self.assertEqual(offline['host'],'offline.local')
            self.assertEqual(offline['jobs'],{})
            self.assertEqual(offline['exclusion_stage'],'launcher_verification')

    def test_retry_keeps_previous_worker_eligible(self):
        with tempfile.TemporaryDirectory() as folder:
            service=DistributedTests(state=folder)
            original=plan([{'host':name+'.local'} for name in ('a','b')])
            service.start(selected_hosts=original,launch=False)
            service.run['avoid_workers']=['a.local']
            with patch('agent_client.AgentSessions') as sessions, patch('distributed.ImageDistributor'), patch('github_sources.publish',side_effect=RuntimeError('stop after planning')):
                sessions.return_value.accessible.return_value=original
                sessions.return_value.verify.return_value=original
                service.execute()
            sessions.return_value.verify.assert_called_once_with(original)
            self.assertEqual(service.run['excluded_hosts'],[])

    def test_github_urls_and_pinned_source_manifest(self):
        from github_sources import github_url,source_digest
        self.assertEqual(github_url('git@github.com:owner/Organization.git'),'https://github.com/owner/Organization.git')
        self.assertEqual(github_url('https://github.com/owner/PIdP'),'https://github.com/owner/PIdP.git')
        for url in ('https://token@github.com/owner/repo','file:///tmp/repo','https://elsewhere.example/repo'):
            with self.assertRaises(ValueError):github_url(url)
        refs={'.':{'url':'https://github.com/owner/Organization.git','commit':'a'*40,'stage':'pushed'}}
        before=source_digest(refs)
        refs['.']['stage']='verified'
        self.assertEqual(before,source_digest(refs))
        refs['.']['commit']='b'*40
        self.assertNotEqual(before,source_digest(refs))

    def test_history_shows_ten_independent_records_without_replacing_older_results(self):
        with tempfile.TemporaryDirectory() as folder:
            service=DistributedTests(state=folder)
            ids=[]
            for index in range(12):
                ident='20261024T120000Z-'+format(index,'08x');ids.append(ident)
                service.run={'id':ident,'started':'2026-10-24T12:00:00Z','status':'passed',
                             'hosts':[{'jobs':{'monitor':{'status':'passed','logs':['run '+str(index)]}}}]}
                service.save()
            view=service.view()
            self.assertEqual(len(view['history']),10)
            self.assertEqual(view['history'][0]['id'],ids[-1])
            self.assertEqual(view['history'][0]['passed'],1)
            old=service.view(ids[0])['run']
            self.assertEqual(old['hosts'][0]['jobs']['monitor']['logs'],['run 0'])
            self.assertEqual(service.run['id'],ids[-1])
            self.assertEqual(len(list(Path(folder).glob('*.json'))),12)

    def test_restart_marks_jobs_interrupted_and_history_ids_cannot_escape(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / '20261023T120000Z-1234abcd.json'
            path.write_text(json.dumps({'id': path.stem, 'status': 'running', 'hosts': [{'status': 'running', 'jobs': {'a': {'status': 'running'}}}]}))
            service = DistributedTests(state=folder)
            self.assertEqual(service.view()['run']['hosts'][0]['jobs']['a']['status'], 'interrupted')
            with self.assertRaises(ValueError): service.view('../../etc/passwd')

if __name__ == '__main__': unittest.main()
