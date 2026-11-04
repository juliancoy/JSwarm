import copy
import json
from pathlib import Path
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from test_scope import normalize, select
from test_requests import TestRequests


class ScopeTests(unittest.TestCase):
    def test_internal_retry_of_one_build_check_does_not_expand_to_all_component_tests(self):
        specs={'test':{'base':'web','file':'src/one.test.ts'},'build':{'base':'web','check':'build'}}
        self.assertEqual(select(list(specs),specs,{'paths':[],'suites':['web'],'task_ids':['build']}),['build'])
        with self.assertRaises(ValueError):select(list(specs),specs,{'paths':[],'suites':['web'],'task_ids':['absent']})

    def setUp(self):
        self.specs = {'one': {'base': 'monitor', 'file': 'tests/test_one.py'},
                      'two': {'base': 'monitor', 'file': 'tests/test_two.py'},
                      'map': {'base': 'map', 'file': 'tests/ui.test.js'},
                      'web': {'base': 'web', 'file': 'src/app.test.ts'},
                      'build': {'base': 'web', 'check': 'build'}, 'visual': {'base': 'visual'}}

    def test_exact_test_selects_only_that_file(self):
        self.assertEqual(select(list(self.specs), self.specs, normalize(['swarm-control/server/tests/test_one.py'])), ['one'])

    def test_component_source_includes_checks_but_no_unrelated_components(self):
        self.assertEqual(select(list(self.specs), self.specs, normalize(['OrgPortal/web/src/app.ts'])), ['web', 'build'])

    def test_suite_and_path_are_unioned(self):
        self.assertEqual(select(list(self.specs), self.specs, normalize(['swarm-control/server/tests/test_one.py'], ['web'])), ['one','web','build'])

    def test_usajobs_source_selects_the_real_codecollective_browser_suite(self):
        specs = {'mobile': {'base': 'usajobs', 'file': 'tests/usajobs-mobile.test.cjs'}}
        self.assertEqual(select(list(specs), specs, normalize(['CodeCollective/usa/index.html'])), ['mobile'])
        self.assertEqual(select(list(specs), specs, normalize([], ['usajobs'])), ['mobile'])

    def test_invalid_selection_fails_closed(self):
        for value in (['../secret'], ['/tmp/test.py'], ['a/../../b']):
            with self.assertRaises(ValueError): normalize(value)
        with self.assertRaises(ValueError): normalize([], ['arbitrary-command'])
        with self.assertRaises(ValueError): normalize()
        with self.assertRaises(ValueError): select(list(self.specs), self.specs, normalize(['unknown/file.py']))

    def test_catalog_only_reads_repositories_needed_by_selected_bases(self):
        from work_queue import catalog
        from distributed_worker import SUITES
        specs=copy.deepcopy(SUITES)
        with patch('work_queue.subprocess.check_output',return_value=b'swarm-control/server/tests/test_one.py\0') as git:
            tasks=catalog('/workspace',specs,{'.':{'commit':'abc'}},bases={'monitor'})
        self.assertEqual(len(git.call_args_list),1)
        self.assertEqual(git.call_args.args[0][2],'/workspace')
        self.assertTrue(any(task.startswith('monitor--') for task in tasks))

    def test_focused_result_depends_on_selected_jobs_not_unused_workers(self):
        from distributed import outcome
        run={'selection':normalize([],['monitor']),'granular':True,'pending_tasks':[]}
        hosts=[{'status':'passed','jobs':{'test':{'status':'passed'}}},{'status':'unavailable','jobs':{}}]
        self.assertEqual(outcome(run,hosts,{}),'passed')
        run['pending_tasks']=['unexecuted']
        self.assertEqual(outcome(run,hosts,{}),'failed')
        run['pending_tasks']=[];hosts[0]['jobs']['test']['status']='unavailable'
        self.assertEqual(outcome(run,hosts,{}),'failed')

    def test_p2p_selection_does_not_add_ordinary_tests(self):
        self.assertEqual(select(list(self.specs), self.specs, normalize([], ['p2p'])), [])


class InboxTests(unittest.TestCase):
    def test_active_request_does_not_block_later_requests_and_records_runtime(self):
        with tempfile.TemporaryDirectory() as folder:
            runner = SimpleNamespace(folder=Path(folder), running=True)
            tests = SimpleNamespace(runner=runner, lock=threading.RLock(), reruns={}, view=Mock(),create_run=Mock(return_value=None))
            queue = TestRequests(tests)
            record = {'id':'00000000-0000-0000-0000-000000000001', 'submitted_at':'2026-01-01T00:00:00+00:00',
                      'status':'queued','selection':normalize([],['monitor']),'run_id':None}
            queue.save(record); queue.tick()
            self.assertEqual(queue.view(record['id'])['status'], 'queued')
            record.update(status='running', run_id='run')
            queue.save(record)
            tests.view.return_value={'run':{'status':'passed','started':'2026-01-01T00:00:02+00:00','finished':'2026-01-01T00:00:05+00:00'}}
            queue.tick()
            saved=queue.view(record['id'])
            self.assertEqual(saved['status'],'passed');self.assertEqual(saved['duration_seconds'],3)
            restarted=TestRequests(tests)
            self.assertEqual(restarted.view(record['id']),saved)

    def test_run_is_saved_before_execution_dispatch(self):
        with tempfile.TemporaryDirectory() as folder:
            runner=Mock(folder=Path(folder),running=False,run={'id':'new-run'})
            tests=SimpleNamespace(runner=runner,lock=threading.RLock(),reruns={},create_run=Mock(return_value=runner))
            queue=TestRequests(tests)
            record={'id':'00000000-0000-0000-0000-000000000001','submitted_at':'2026-01-01T00:00:00+00:00',
                    'status':'queued','selection':normalize([],['map']),'run_id':None}
            queue.save(record)
            with patch('test_requests.threading.Thread') as thread:
                thread.return_value.start.side_effect=lambda: self.assertEqual(queue.view(record['id'])['run_id'],'new-run')
                queue.tick()
            tests.create_run.assert_called_once_with(force=False,selection=record['selection'])
