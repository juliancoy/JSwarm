import tempfile
from pathlib import Path
from unittest import TestCase
from unittest.mock import Mock, patch

from worker_preparation import Preparation, execution_capacity
from work_queue import claim


class PreparationTests(TestCase):
    def test_ready_jobs_bypass_blocked_long_job(self):
        specs={'long':{'base':'web','cpus':2,'memory_mb':3072},
               'short':{'base':'monitor','cpus':1,'memory_mb':512}}
        queue=list(specs)
        self.assertEqual(claim(queue,specs,{'cpus':4,'memory_mb':8192},set(),{'monitor'}),'short')
        self.assertEqual(queue,['long'])

    def test_reservations_release_without_exceeding_capacity(self):
        capacity={'cpus':4,'memory_mb':8192}
        self.assertEqual(execution_capacity(capacity,True,True),{'cpus':1,'memory_mb':4608})
        self.assertEqual(execution_capacity(capacity,True,False),{'cpus':3,'memory_mb':6656})
        self.assertEqual(execution_capacity(capacity),capacity)
        self.assertEqual(execution_capacity({'cpus':1,'memory_mb':1024},True,True),{'cpus':0,'memory_mb':0})

    def test_dependency_free_jobs_ready_before_installs_and_errors_are_reported(self):
        specs={'web':{'prepare':['npm'],'image':'node'},'monitor':{'image':'python'}}
        dependencies=Mock()
        dependencies.ensure.side_effect=[0,RuntimeError('installer failed')]
        with tempfile.TemporaryDirectory() as folder, patch('worker_preparation.SUITES',specs), patch('worker_preparation.emit') as emit:
            prep=Preparation(Path(folder),{}, {'suites':['web','monitor'],'images':{'node':'node','python':'python'}},dependencies,{'cpus':4,'memory_mb':8192})
            prep.prepare()
            self.assertEqual([call.args[0] for call in dependencies.ensure.call_args_list],['monitor','web'])
            self.assertEqual(prep.results,{'monitor':0,'web':125})
            self.assertEqual(prep.ready,{'monitor','web'})
            self.assertFalse(prep.running)
            self.assertTrue(any(call.kwargs.get('status')=='failed' for call in emit.call_args_list))

    def test_idle_worker_cancels_unneeded_installer_promptly(self):
        import sys
        import threading
        import time
        from distributed_worker import run
        cancel=threading.Event()
        timer=threading.Timer(.1,cancel.set)
        with tempfile.TemporaryDirectory() as folder, patch('distributed_worker.emit'):
            started=time.monotonic()
            timer.start()
            try:
                code=run(Path(folder),'setup','prepare',{'native':True,'cwd':'.',
                         'prepare':[sys.executable,'-c','import time; time.sleep(30)']},'test',{},cancel=cancel)
            finally:timer.cancel()
        self.assertEqual(code,130)
        self.assertLess(time.monotonic()-started,3)
