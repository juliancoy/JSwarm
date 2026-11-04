import concurrent.futures
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from granular_worker import Dependencies, capacity, check_command


class DependencySetupTests(unittest.TestCase):
    def test_concurrent_files_install_a_shared_package_only_once(self):
        with tempfile.TemporaryDirectory() as folder:
            setup=Dependencies(Path(folder),{'id':'run'}, {})
            spec={'base':'org-api','image':'immutable-node','cwd':'OrgPortal/org-worker'}
            with patch('granular_worker.run',return_value=0) as run, patch('granular_worker.emit'):
                with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
                    results=list(pool.map(lambda n:setup.ensure('org-api--'+str(n),spec),range(20)))
                self.assertEqual(results,[0]*20)
                self.assertEqual(run.call_count,2)  # chat dependency and org-worker
                self.assertEqual({call.args[3]['cwd'] for call in run.call_args_list},
                                 {'OrgPortal/chat-worker','OrgPortal/org-worker'})

    def test_only_bundle_stages_public_data_with_sufficient_memory(self):
        with tempfile.TemporaryDirectory() as folder:
            setup=Dependencies(Path(folder),{'id':'run'}, {})
            spec={'base':'web','image':'immutable-node','cwd':'OrgPortal/web','memory_mb':1024}
            with patch('granular_worker.run',return_value=0) as run, patch('granular_worker.emit'):
                self.assertEqual(setup.ensure('web--unit',spec),0)
                self.assertEqual(run.call_count,2)
                self.assertEqual(setup.ensure('web--build-bundle',spec),0)
                self.assertEqual(run.call_count,3)
                staging=run.call_args.args[3]
                self.assertEqual(staging['memory_mb'],3072)
                self.assertIn('scripts/stage-ecosystem-build.mjs',staging['prepare'])
                self.assertEqual(setup.ensure('web--build-bundle',spec),0)
                self.assertEqual(run.call_count,3)

    def test_failed_install_is_reported_to_every_dependent_file(self):
        with tempfile.TemporaryDirectory() as folder:
            setup=Dependencies(Path(folder),{'id':'run'}, {})
            spec={'base':'identity-python','image':'python','cwd':'PIdP'}
            with patch('granular_worker.run',return_value=17) as run, patch('granular_worker.emit'):
                self.assertEqual(setup.ensure('first',spec),17)
                self.assertEqual(setup.ensure('second',spec),17)
                run.assert_called_once()


class CapacityTests(unittest.TestCase):
    def test_file_concurrency_respects_affinity_and_caps_docker_pressure(self):
        with patch('granular_worker.Path.read_text',return_value='MemAvailable: 8388608 kB\n'):
            with patch('granular_worker.os.sched_getaffinity',return_value={0,1}):
                self.assertEqual(capacity()['cpus'],2)
            with patch('granular_worker.os.sched_getaffinity',return_value=set(range(32))):
                self.assertEqual(capacity()['cpus'],4)
                self.assertEqual(capacity()['memory_mb'],7168)


def test_bundle_execution_keeps_offline_snapshot_and_suppresses_metadata_rewrites():
    assert check_command({'base':'web','check':'build:bundle'})==['env','ORGPORTAL_ECOSYSTEM_BUILD_SNAPSHOT=/work/.ecosystem-build-snapshot.json','npm','--ignore-scripts','run','build:bundle']
    assert check_command({'base':'web','check':'typecheck'})==['npm','--ignore-scripts','run','typecheck']


def test_bundle_check_uses_application_owned_build_after_script_rename(tmp_path):
    import json
    (tmp_path/'package.json').write_text(json.dumps({'scripts':{'build':'node scripts/build.mjs'}}))
    command=check_command({'base':'web','check':'build:bundle'},tmp_path)
    assert command[-1]=='build'
    assert 'ORGPORTAL_ECOSYSTEM_BUILD_SNAPSHOT=/work/.ecosystem-build-snapshot.json' in command
