import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from distributed_worker import SUITES
from work_queue import catalog, claim, pending


class FileQueueTests(unittest.TestCase):
    def test_catalog_uses_pinned_trees_and_covers_root_and_nested_files_once(self):
        trees = {
            '.': ['swarm-control/server/tests/test_monitor.py', 'swarm-control/server/tests/model.test.js', 'tests/test_tool.py'],
            'OrgPortal': ['org-worker/test/api.test.ts', 'chat-worker/test/chat.test.ts',
                          'web/src/root.test.ts', 'web/src/nested/component.test.tsx', 'web/src/not-a-test.ts'],
            'CodeCollective': ['tests/usajobs-mobile.test.cjs'],
            'PIdP': ['serverless/test/identity.test.mjs', 'tests/test_one.py', 'tests/nested/two_test.py'],
        }
        repos = {name: {'commit': name+'-pinned'} for name in trees}
        calls = []
        def tree(command, **kwargs):
            calls.append(command)
            name = Path(command[2]).name
            repo = name if name in ('OrgPortal','PIdP','CodeCollective') else '.'
            return '\0'.join(trees[repo]).encode()
        specs = copy.deepcopy(SUITES)
        with patch('work_queue.subprocess.check_output', side_effect=tree):
            tasks = catalog('/checkout', specs, repos)
        files = [specs[task].get('file') for task in tasks if specs[task].get('file')]
        self.assertEqual(len(tasks), len(set(tasks)))
        self.assertIn('tests/usajobs-mobile.test.cjs', files)
        mobile = next(specs[task] for task in tasks if specs[task].get('base') == 'usajobs')
        self.assertNotIn('tsx', mobile['test'])
        self.assertEqual(mobile['cwd'], 'CodeCollective')
        self.assertIn('src/root.test.ts', files)
        self.assertIn('src/nested/component.test.tsx', files)
        self.assertIn('tests/nested/two_test.py', files)
        self.assertNotIn('src/not-a-test.ts', files)
        self.assertEqual(len(files), 11)
        for task in tasks:
            if specs[task].get('base') in ('monitor','workspace','identity-python'):
                self.assertEqual(specs[task]['test'][:3], ['python','-m','pytest'])
                self.assertIn('pytest', specs[task]['prepare'])
        self.assertEqual({call[-1] for call in calls}, {r['commit'] for r in repos.values()})
        self.assertIn('web--build-bundle', tasks)
        self.assertIn('web--typecheck', tasks)
        self.assertEqual(specs['web--build-bundle']['check'], 'build:bundle')
        self.assertEqual(specs['web--build-bundle']['test'], ['npm', 'run', 'build:bundle'])
        self.assertEqual(specs['web--typecheck']['test'], ['npm', 'run', 'typecheck'])
        self.assertIn('org-api--check-bundle', tasks)
        frontend = next(specs[t] for t in tasks if specs[t].get('file') == 'src/root.test.ts')
        self.assertIn('--config', frontend['test'])

    def test_only_reviewed_docker_fixtures_run_on_the_enrolled_host(self):
        from work_queue import HOST_DOCKER_FIXTURES
        files = [*sorted(HOST_DOCKER_FIXTURES), 'tests/test_pull_all.py']
        specs = copy.deepcopy(SUITES)
        with patch('work_queue.subprocess.check_output', return_value='\0'.join(files).encode()):
            tasks = catalog('/checkout', specs, {'.':{'commit':'reviewed'}}, bases={'workspace'})
        for task in tasks:
            if specs[task].get('base') == 'workspace':
                self.assertEqual(bool(specs[task].get('host_fixture')), specs[task]['file'] in HOST_DOCKER_FIXTURES)
        from distributed_worker import _run
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaisesRegex(ValueError,'explicitly reviewed'):
                _run(Path(folder),'bad','test',{'cwd':'.','base':'workspace',
                    'file':'tests/unknown.py','host_fixture':True,'test':['false']},'run',{})

    def test_claim_fills_spare_memory_and_cpu_without_suite_affinity(self):
        specs = {'long': {'cpus':2,'memory_mb':3072}, 'small': {'cpus':1,'memory_mb':512},
                 'next': {'cpus':1,'memory_mb':512}}
        queue = ['long','small','next']
        small_host = {'cpus':2,'memory_mb':1024}
        large_host = {'cpus':4,'memory_mb':8192}
        self.assertEqual(claim(queue,specs,small_host,set()), 'small')
        self.assertEqual(claim(queue,specs,large_host,set()), 'long')
        self.assertEqual(claim(queue,specs,small_host,{'small'}), 'next')
        self.assertEqual(queue, [])
        self.assertIsNone(claim(['next'],specs,small_host,{'small','next'}))

    def test_finished_worker_claims_another_file_while_slow_worker_is_busy(self):
        specs={str(i):{'cpus':1,'memory_mb':512} for i in range(5)}
        queue=list(specs)
        capacity={'cpus':1,'memory_mb':512}
        slow=claim(queue,specs,capacity,set())
        fast=claim(queue,specs,capacity,set())
        self.assertIsNone(claim(queue,specs,capacity,{slow}))
        next_fast=claim(queue,specs,capacity,set())
        self.assertNotEqual(fast,next_fast)
        self.assertEqual(len(queue),2)

    def test_successful_execution_time_prioritizes_long_files_not_setup_time(self):
        with tempfile.TemporaryDirectory() as folder:
            jobs={'fast':{'status':'passed','duration_seconds':500,'phase_seconds':{'test':1}},
                  'slow':{'status':'passed','duration_seconds':20,'phase_seconds':{'test':20}}}
            (Path(folder)/'run.json').write_text(json.dumps({'hosts':[{'jobs':jobs}]}))
            self.assertEqual(pending(folder,['fast','slow']),['slow','fast'])
