import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock,patch

from distributed_worker import prepare_package,dependency_cache,run

class DependencyArchiveTests(unittest.TestCase):
    def test_changed_install_inputs_keep_the_shared_download_mount(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);work=self.setup_work(root,'one')
            spec={'cwd':'OrgPortal/web','image':'sha256:a','prepare':['npm','ci']}
            process=Mock();process.stdout=[];process.poll.return_value=0;process.wait.return_value=0
            env={'PIDP_BUILD_COMMIT':'a','ORGPORTAL_BUILD_COMMIT':'b'}
            with patch('distributed_worker.Path.home',return_value=root/'home'), \
                 patch('distributed_worker.subprocess.Popen',return_value=process) as launch, \
                 patch('distributed_worker.subprocess.run'),patch('distributed_worker.emit'), \
                 patch('scripts.launcher_agent.ResourceLease',return_value=Mock(try_acquire=Mock(return_value=True))):
                run(work,'web','prepare',spec,'one',env)
                run(work,'identity','prepare',{**spec,'image':'sha256:b'},'two',env)
            mounts=[[arg for arg in call.args[0] if arg.endswith(',dst=/cache')] for call in launch.call_args_list]
            self.assertEqual(mounts[0],mounts[1])
            self.assertIn('packages/downloads',mounts[0][0])

    def setup_work(self,root,name,lock='one'):
        work=root/name/'source';cwd=work/'OrgPortal/web';cwd.mkdir(parents=True)
        (cwd/'package-lock.json').write_text(json.dumps({'packages':{},'fixture':lock}))
        (cwd/'package.json').write_text('{}')
        return work

    def test_install_is_reused_in_private_copies_and_metadata_is_regenerated(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);one=self.setup_work(root,'one');two=self.setup_work(root,'two')
            spec={'cwd':'OrgPortal/web','image':'sha256:fixture','prepare':['sh','-c','npm ci --no-audit && npm run prebuild']}
            calls=[]
            def runner(work,task,phase,setup,*args,**kw):
                calls.append(setup['prepare'])
                if setup['prepare'][2].startswith('npm ci'):
                    node=work/'OrgPortal/web/node_modules';node.mkdir();(node/'fixture').write_text('installed')
                return 0
            with patch('distributed_worker.Path.home',return_value=root/'home'),patch('distributed_worker.emit'):
                self.assertEqual(prepare_package(one,'web','web',spec,'one',{},runner),0)
                self.assertEqual(prepare_package(two,'web','web',spec,'two',{},runner),0)
            self.assertEqual(calls,[spec['prepare'],['sh','-c','npm run prebuild']])
            (two/'OrgPortal/web/node_modules/fixture').write_text('modified')
            self.assertEqual((one/'OrgPortal/web/node_modules/fixture').read_text(),'installed')

    def test_lock_runtime_and_command_changes_get_distinct_caches(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);work=self.setup_work(root,'one')
            spec={'cwd':'OrgPortal/web','image':'sha256:a','prepare':['npm','ci']}
            with patch('distributed_worker.Path.home',return_value=root/'home'):
                keys={dependency_cache(work,'web',spec),dependency_cache(work,'web',{**spec,'image':'sha256:b'}),dependency_cache(work,'web',{**spec,'prepare':['npm','install']})}
                (work/'OrgPortal/web/package-lock.json').write_text('{}')
                keys.add(dependency_cache(work,'web',spec))
            self.assertEqual(len(keys),4)

    def test_corrupt_archive_is_reinstalled_and_failed_installs_are_not_cached(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);work=self.setup_work(root,'one')
            spec={'cwd':'OrgPortal/web','image':'sha256:fixture','prepare':['sh','-c','npm ci --no-audit']}
            with patch('distributed_worker.Path.home',return_value=root/'home'):
                cache=dependency_cache(work,'web',spec);archive=cache/'installed-v1.tar.gz';archive.write_text('broken')
                calls=[]
                def runner(*args,**kwargs):calls.append(args);return 17
                self.assertEqual(prepare_package(work,'web','web',spec,'one',{},runner),17)
                self.assertFalse(archive.exists());self.assertEqual(len(calls),1)

    def test_failed_or_cancelled_jobs_do_not_start_installs(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);work=self.setup_work(root,'one');(work.parent/'STOP').touch()
            spec={'cwd':'OrgPortal/web','image':'sha256:a','prepare':['sh','-c','npm ci --no-audit']}
            with patch('distributed_worker.Path.home',return_value=root/'home'):
                self.assertEqual(prepare_package(work,'web','web',spec,'one',{},lambda *a,**kw:self.fail('Installer must not run')),130)

    def test_parallel_jobs_share_one_install_but_not_writable_trees(self):
        import concurrent.futures
        import threading
        import time
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);works=[self.setup_work(root,str(i)) for i in range(2)]
            spec={'cwd':'OrgPortal/web','image':'sha256:a','prepare':['sh','-c','npm ci --no-audit']}
            installed=[];guard=threading.Lock()
            def runner(work,task,phase,setup,*args,**kwargs):
                if setup.get('prepare'):
                    with guard:installed.append(work)
                    time.sleep(.03)
                    node=work/'OrgPortal/web/node_modules';node.mkdir();(node/'fixture').write_text('installed')
                return 0
            with patch('distributed_worker.Path.home',return_value=root/'home'),patch('distributed_worker.emit'):
                with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                    results=list(pool.map(lambda work:prepare_package(work,'web','web',spec,'fixture',{},runner),works))
            self.assertEqual(results,[0,0]);self.assertEqual(len(installed),1)
            self.assertTrue(all((work/'OrgPortal/web/node_modules/fixture').exists() for work in works))

    def test_archive_cannot_extract_outside_its_private_staging_directory(self):
        import io
        import tarfile
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);work=self.setup_work(root,'one')
            spec={'cwd':'OrgPortal/web','image':'sha256:a','prepare':['sh','-c','npm ci --no-audit']}
            with patch('distributed_worker.Path.home',return_value=root/'home'):
                cache=dependency_cache(work,'web',spec)
                with tarfile.open(cache/'installed-v1.tar.gz','w:gz') as tar:
                    entry=tarfile.TarInfo('node_modules/../../escape');entry.size=7;tar.addfile(entry,io.BytesIO(b'escaped'))
                installer=Mock(return_value=17)
                self.assertEqual(prepare_package(work,'web','web',spec,'fixture',{},installer),17)
                installer.assert_called_once()
                self.assertFalse((work/'OrgPortal/escape').exists())
                self.assertFalse((work/'OrgPortal/web/escape').exists())
