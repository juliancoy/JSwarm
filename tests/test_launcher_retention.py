import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0,str((Path(__file__).resolve().parents[2] if Path(__file__).resolve().parents[1].name in ('HarborGenetics', 'JSwarm') else Path(__file__).resolve().parents[1])/'scripts'))
from launcher_agent import prune_workspaces

class RetentionTests(unittest.TestCase):
    def test_unchanged_terminal_receipts_are_not_rewritten_each_sweep(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);self.job(root,1)
            prune_workspaces(root,now=100000,min_free=0)
            with patch('launcher_agent.write_job_state') as write:
                prune_workspaces(root,now=100001,min_free=0)
            write.assert_not_called()

    def test_concurrent_admission_does_not_wait_for_an_existing_storage_sweep(self):
        import threading
        from launcher_agent import Agent
        agent=Agent.__new__(Agent);agent.storage_lock=threading.Lock();agent.storage_status={'checked_at':100}
        agent.storage_lock.acquire()
        try:
            with patch('docker.from_env') as docker:
                self.assertEqual(agent.cleanup_workspaces(),{'checked_at':100})
            docker.assert_not_called()
        finally:agent.storage_lock.release()

    def job(self,root,number,status='completed',age=0,prepared=False):
        folder=root/f'20261025T010000Z-{number:08x}';(folder/'source/node_modules').mkdir(parents=True)
        (folder/'source/node_modules/file').write_bytes(b'x'*8192)
        (folder/'manifest.json').write_text('{}')
        (folder/'state.json').write_text(json.dumps({'status':status,'finished_at':100000-age,'prepare_only':prepared}))
        return folder

    def test_persisted_success_is_cleaned_and_results_survive(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);folder=self.job(root,1)
            report=folder/'source/visual-artifacts';report.mkdir();(report/'review.json').write_text('{"passed":true}')
            result=prune_workspaces(root,now=100000,min_free=0)
            self.assertFalse((folder/'source').exists());self.assertTrue((folder/'manifest.json').exists())
            self.assertTrue((folder/'results/visual-artifacts/review.json').exists())
            self.assertEqual(result['removed'],[folder.name])
            self.assertFalse(json.loads((folder/'state.json').read_text())['workspace_retained'])

    def test_active_mounted_unknown_and_symlink_jobs_are_never_removed(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);active=self.job(root,1);mounted=self.job(root,2);running=self.job(root,3,'running')
            linked=root/'20261025T010000Z-00000004';linked.symlink_to(mounted,target_is_directory=True)
            prune_workspaces(root,active=[active.name],mounts=[str(mounted/'source')],now=100000,budget=0,min_free=0)
            for folder in [active,mounted,running]:self.assertTrue((folder/'source').exists())
            self.assertTrue(linked.is_symlink())

    def test_only_two_recent_failures_and_one_warm_preparation_survive(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            failures=[self.job(root,i,'failed',age=i*100) for i in range(1,4)]
            expired=self.job(root,4,'failed',age=86401)
            warm=self.job(root,5,prepared=True,age=100);old=self.job(root,6,prepared=True,age=1900)
            prune_workspaces(root,now=100000,min_free=0)
            self.assertTrue(all((f/'source').exists() for f in failures[:2]+[warm]))
            self.assertTrue(all(not (f/'source').exists() for f in [failures[2],expired,old]))

    def test_byte_budget_evicts_oldest_and_low_space_evicts_all_finished(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);old=self.job(root,1,'failed',age=200);new=self.job(root,2,'failed',age=100)
            prune_workspaces(root,now=100000,budget=9000,min_free=0)
            self.assertFalse((old/'source').exists());self.assertTrue((new/'source').exists())
            with patch('launcher_agent.shutil.disk_usage') as usage:
                usage.return_value.free=0
                prune_workspaces(root,now=100000,min_free=1)
            self.assertFalse((new/'source').exists())

    def test_artifact_symlinks_do_not_copy_private_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);folder=self.job(root,1)
            (root/'private').write_text('secret');report=folder/'source/visual-artifacts';report.mkdir()
            (report/'secret.json').symlink_to(root/'private')
            prune_workspaces(root,now=100000,min_free=0)
            self.assertFalse((folder/'results/visual-artifacts/secret.json').exists())

    def test_periodic_cleanup_does_not_extend_failure_retention(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);folder=self.job(root,1,'failed')
            prune_workspaces(root,now=100000,min_free=0)
            prune_workspaces(root,now=100000+86401,min_free=0)
            self.assertFalse((folder/'source').exists())

    def test_report_parent_symlink_is_not_followed(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);folder=self.job(root,1)
            private=root/'private';(private/'test-status').mkdir(parents=True);(private/'test-status/secret.json').write_text('private')
            (folder/'source/.local').symlink_to(private,target_is_directory=True)
            prune_workspaces(root,now=100000,min_free=0)
            self.assertFalse((folder/'results/.local-test-status').exists())
            self.assertTrue((private/'test-status/secret.json').exists())

    def test_shared_cache_is_bounded_and_mounted_entries_survive(self):
        from launcher_agent import prune_package_cache
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            for name in ['old','active']:
                (root/name).mkdir();(root/name/'blob').write_bytes(b'x'*8192)
            prune_package_cache(root,mounts=[str(root/'active')],budget=0)
            self.assertFalse((root/'old').exists());self.assertTrue((root/'active/blob').exists())

    def test_low_disk_space_refuses_new_jobs_after_attempting_cleanup(self):
        import threading
        from unittest.mock import Mock
        from launcher_agent import Agent
        with tempfile.TemporaryDirectory() as directory:
            agent=Agent.__new__(Agent);agent.root=Path(directory);agent.jobs={};agent.lock=threading.RLock();agent.cleanup_workspaces=Mock()
            with patch('launcher_agent.shutil.disk_usage') as usage:
                usage.return_value.free=0
                with self.assertRaisesRegex(RuntimeError,'10 GiB'):
                    agent.start({'id':'20261025T010000Z-00000001'})
            agent.cleanup_workspaces.assert_called_once()
            self.assertEqual(list(agent.root.iterdir()),[])
