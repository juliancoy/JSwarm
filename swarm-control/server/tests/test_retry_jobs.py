from pathlib import Path
import tempfile
import unittest
from retry_jobs import RetryQueue,blocked,unexecuted,retained_parent
from task_events import EventStore

class RetryQueueTests(unittest.TestCase):
    def test_unassigned_catalog_jobs_survive_worker_failure_without_charging_an_attempt(self):
        parent={'status':'failed','task_catalog':['monitor--pending','monitor--done'],'pending_tasks':['monitor--pending','monitor--done','not-in-catalog'],
                'hosts':[{'jobs':{'monitor--done':{'status':'passed'}}}]}
        self.assertEqual(unexecuted(parent),['monitor--pending'])
        self.assertEqual(unexecuted({**parent,'stop_requested':True}),[])
        with tempfile.TemporaryDirectory() as directory:
            q=RetryQueue(EventStore(Path(directory)/'db'));q.offer({'id':'parent'},'monitor--pending',attempt=0)
            ticket=q.claim(now=10**12);q.dispatched(ticket['id'],'child')
            self.assertEqual(q.rows()[0]['attempts'],1)

    def test_batches_share_one_pinned_parent_and_claim_atomically(self):
        with tempfile.TemporaryDirectory() as directory:
            queue=RetryQueue(EventStore(Path(directory)/'db'))
            for task in ('one','two','three'):queue.offer({'id':'parent'},task)
            queue.offer({'id':'other-commit'},'four')
            with queue.store.connect() as db:db.execute("UPDATE retry_jobs SET next_due=CASE WHEN parent_run='parent' THEN 1 ELSE 2 END")
            tickets=queue.claim_batch(2,now=10**12)
            self.assertEqual(len(tickets),2)
            self.assertEqual(len({t['parent_run'] for t in tickets}),1)
            self.assertEqual({t['state'] for t in queue.rows() if t['id'] in {r['id'] for r in tickets}},{'claiming'})
            queue.dispatched_batch([t['id'] for t in tickets],'shared-child')
            for row in queue.rows():
                if row['id'] in {t['id'] for t in tickets}:
                    self.assertEqual((row['state'],row['child_run']),('running','shared-child'))
                    self.assertIsNone(row['reason']);self.assertIsNone(row['next_due'])

    def test_retained_parent_recovery_checks_pinned_identity_and_source(self):
        import json
        from github_sources import source_digest
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);store=EventStore(root/'db')
            repos={'.':{'url':'https://github.com/example/repo.git','commit':'a'*40}}
            parent={'id':'original','repositories':repos,'source_sha256':source_digest(repos)}
            path=root/'original.json';path.write_text(json.dumps(parent))
            self.assertEqual(retained_parent(store,root,'original'),parent)
            self.assertEqual(store.list_runs('application-tests'),[parent])
            path.write_text(json.dumps({**parent,'source_sha256':'changed'}))
            with self.assertRaisesRegex(ValueError,'fingerprint'):retained_parent(store,root,'original')
            with self.assertRaisesRegex(ValueError,'unavailable'):retained_parent(store,root,'missing')
            with self.assertRaisesRegex(ValueError,'identity'):retained_parent(store,root,'../original')

    def test_batch_retries_combine_files_without_republishing_sources(self):
        from live_distributed import LiveDistributedTests
        from github_sources import source_digest
        from types import SimpleNamespace
        from unittest.mock import Mock
        service=LiveDistributedTests.__new__(LiveDistributedTests);runner=SimpleNamespace(run={},save=Mock());service.create_run=Mock(return_value=runner)
        repos={'.':{'url':'https://github.com/example/repo.git','commit':'a'*40}}
        parent={'id':'parent','repositories':repos,'source_sha256':source_digest(repos),'hosts':[{'host':'peer','jobs':{
            'monitor--one':{'suite':'monitor','file':'tests/test_history.py'},'monitor--two':{'suite':'monitor','file':'tests/test_agent_sessions.py'}}}]}
        tickets=[{'id':'parent:'+task,'task':task,'attempts':1} for task in ('monitor--one','monitor--two')]
        service.retry_pinned(parent,{**tickets[0],'batch':tickets})
        self.assertEqual(len(service.create_run.call_args.kwargs['selection']['paths']),2)
        self.assertEqual(runner.run['pinned_repositories'],repos)
        self.assertEqual(runner.run['retry_tickets'],[t['id'] for t in tickets])

    def test_mixed_assigned_and_unassigned_retry_batch_uses_pinned_catalog(self):
        from live_distributed import LiveDistributedTests
        from github_sources import source_digest
        from types import SimpleNamespace
        from unittest.mock import Mock,patch
        service=LiveDistributedTests.__new__(LiveDistributedTests);service.runner=SimpleNamespace(root='/workspace')
        runner=SimpleNamespace(run={},save=Mock());service.create_run=Mock(return_value=runner)
        repos={'.':{'url':'https://github.com/example/repo.git','commit':'a'*40}}
        parent={'id':'parent','repositories':repos,'source_sha256':source_digest(repos),'task_catalog':['monitor--one','monitor--two'],
                'hosts':[{'host':'peer','jobs':{'monitor--one':{'suite':'monitor','file':'tests/one.py'}}}]}
        def catalog(root,specs,repositories):
            self.assertEqual(repositories,repos);specs['monitor--two']={'base':'monitor','file':'tests/two.py'};return ['monitor--two']
        tickets=[{'id':'parent:'+task,'task':task,'attempts':attempt} for task,attempt in [('monitor--one',1),('monitor--two',0)]]
        with patch('work_queue.catalog',side_effect=catalog):service.retry_pinned(parent,{**tickets[0],'batch':tickets})
        self.assertEqual(service.create_run.call_args.kwargs['selection']['task_ids'],['monitor--one','monitor--two'])
        self.assertEqual(len(service.create_run.call_args.kwargs['selection']['paths']),2)

    def test_claim_backoff_budget_and_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            store=EventStore(Path(directory)/'events.db');queue=RetryQueue(store)
            queue.offer({'id':'run'},'suite')
            self.assertIsNone(queue.claim())
            ticket=queue.claim(now=10**12);self.assertEqual(ticket['attempts'],1)
            # An unexpired admission claim cannot be taken by a second scheduler.
            self.assertIsNone(RetryQueue(store).claim(now=10**12+1))
            queue.dispatched(ticket['id'],'child');queue.finish(ticket['id'],False)
            ticket=queue.claim(now=10**12);self.assertEqual(ticket['attempts'],2)
            queue.dispatched(ticket['id'],'child2');queue.finish(ticket['id'],False)
            self.assertEqual(queue.rows()[0]['state'],'exhausted');self.assertIsNone(queue.claim(now=10**12))
    def test_duplicate_offer_and_permanent_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            queue=RetryQueue(EventStore(Path(directory)/'db'))
            queue.offer({'id':'run'},'suite',blocked=True);queue.offer({'id':'run'},'suite')
            self.assertEqual(len(queue.rows()),1);self.assertIsNone(queue.claim(now=10**12))
            self.assertTrue(blocked({'exit_code':137}));self.assertFalse(blocked({'exit_code':1}))

    def test_retry_pins_parent_commits_and_prefers_another_worker(self):
        from live_distributed import LiveDistributedTests
        from github_sources import source_digest
        from types import SimpleNamespace
        from unittest.mock import Mock
        service=LiveDistributedTests.__new__(LiveDistributedTests)
        runner=SimpleNamespace(run={},save=Mock())
        service.create_run=Mock(return_value=runner)
        repos={'.':{'url':'https://github.com/example/repo.git','commit':'a'*40}}
        parent={'id':'parent','repositories':repos,'source_sha256':source_digest(repos),'hosts':[{'host':'failed-peer','jobs':{'monitor--one':{'suite':'monitor','file':'tests/test_agent_sessions.py'}}}]}
        result=service.retry_pinned(parent,{'task':'monitor--one','id':'parent:monitor--one','attempts':1})
        self.assertEqual(result.run['pinned_repositories'],repos)
        self.assertEqual(result.run['retry_attempt'],2)
        self.assertEqual(result.run['avoid_workers'],['failed-peer'])
        self.assertEqual(service.create_run.call_args.kwargs['selection']['suites'],[])
        self.assertIn('test_agent_sessions.py',service.create_run.call_args.kwargs['selection']['paths'][0])
        parent['source_sha256']='invalid';service.create_run.reset_mock()
        with self.assertRaises(ValueError):service.retry_pinned(parent,{'task':'monitor--one','id':'t','attempts':1})
        service.create_run.assert_not_called()

    def test_retry_child_catalog_does_not_erase_original_pinned_task(self):
        from live_distributed import LiveDistributedTests
        from github_sources import source_digest
        from types import SimpleNamespace
        from unittest.mock import Mock,patch
        service=LiveDistributedTests.__new__(LiveDistributedTests);service.runner=SimpleNamespace(root='/workspace')
        runner=SimpleNamespace(run={},save=Mock());service.create_run=Mock(return_value=runner)
        repos={'.':{'url':'https://github.com/example/repo.git','commit':'a'*40}}
        parent={'id':'retry-child','retry_family':'original','repositories':repos,'source_sha256':source_digest(repos),'task_catalog':['monitor--other'],'hosts':[]}
        def catalog(root,specs,repositories):
            specs['monitor--original']={'base':'monitor','file':'tests/original.py'}
            return ['monitor--original']
        with patch('work_queue.catalog',side_effect=catalog):
            service.retry_pinned(parent,{'id':'original:monitor--original','task':'monitor--original','attempts':1})
        self.assertEqual(runner.run['pinned_repositories'],repos)
        self.assertEqual(service.create_run.call_args.kwargs['selection']['task_ids'],['monitor--original'])
