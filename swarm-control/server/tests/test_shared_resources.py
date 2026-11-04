import json
import multiprocessing
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
from types import SimpleNamespace
sys.path.insert(0,str(Path(__file__).resolve().parents[3]/'scripts'))
from launcher_agent import ResourceLease
from live_distributed import LiveDistributedTests
from test_requests import TestRequests


def hold(directory,connection):
    lease=ResourceLease('crashed',2,2048,directory=directory,limits={'cpus':2,'memory_mb':4096})
    connection.send(lease.try_acquire())
    threading.Event().wait(30)


class ResourceTests(unittest.TestCase):
    def test_independent_sessions_share_cpu_and_memory_and_return_capacity(self):
        with tempfile.TemporaryDirectory() as directory:
            limits={'cpus':4,'memory_mb':4096}
            a=ResourceLease('analysis',2,3072,directory=directory,limits=limits)
            b=ResourceLease('tests',2,2048,directory=directory,limits=limits)
            c=ResourceLease('tests',1,512,directory=directory,limits=limits)
            try:
                self.assertTrue(a.try_acquire())
                self.assertFalse(b.try_acquire()) # CPU fits, memory does not.
                self.assertTrue(c.try_acquire()) # Backfill with a fitting task.
                a.close()
                self.assertTrue(b.try_acquire())
            finally:a.close();b.close();c.close()

    def test_new_group_gets_capacity_before_heavy_group_refills(self):
        with tempfile.TemporaryDirectory() as directory:
            limits={'cpus':2,'memory_mb':8192}
            a=ResourceLease('busy',1,512,directory=directory,limits=limits)
            refill=ResourceLease('busy',1,512,directory=directory,limits=limits)
            b=ResourceLease('new',1,512,directory=directory,limits=limits)
            try:
                self.assertTrue(a.try_acquire())
                self.assertFalse(refill.try_acquire())
                self.assertTrue(b.try_acquire())
            finally:a.close();refill.close();b.close()

    def test_old_large_waiter_reserves_headroom(self):
        with tempfile.TemporaryDirectory() as directory:
            limits={'cpus':2,'memory_mb':8192}
            a=ResourceLease('active',1,512,directory=directory,limits=limits)
            large=ResourceLease('large',2,1024,directory=directory,limits=limits)
            small=ResourceLease('small',1,512,directory=directory,limits=limits)
            try:
                self.assertTrue(a.try_acquire())
                large.row['created']-=31
                with large.guard():large.write()
                self.assertFalse(small.try_acquire())
                a.close();self.assertTrue(large.try_acquire())
            finally:a.close();large.close();small.close()

    def test_dead_process_releases_reservations(self):
        with tempfile.TemporaryDirectory() as directory:
            parent,child=multiprocessing.Pipe()
            process=multiprocessing.get_context('spawn').Process(target=hold,args=(directory,child))
            process.start()
            try:
                self.assertTrue(parent.poll(3));self.assertTrue(parent.recv())
                lease=ResourceLease('next',2,2048,directory=directory,limits={'cpus':2,'memory_mb':4096})
                try:
                    self.assertFalse(lease.try_acquire())
                    process.terminate();process.join(3)
                    self.assertTrue(lease.try_acquire())
                finally:lease.close()
            finally:
                if process.is_alive():process.kill();process.join()
                parent.close();child.close()


class ConcurrentInboxTests(unittest.TestCase):
    def test_running_request_does_not_block_other_requests(self):
        with tempfile.TemporaryDirectory() as folder:
            runner=Mock(folder=Path(folder),running=True)
            tests=SimpleNamespace(runner=runner,lock=threading.RLock(),reruns={},view=Mock(return_value={'run':{'finished':None}}),create_run=Mock())
            queue=TestRequests(tests)
            active={'id':'00000000-0000-0000-0000-000000000001','submitted_at':'2026-01-01T00:00:00+00:00','status':'running','run_id':'old'}
            queued={'id':'00000000-0000-0000-0000-000000000002','submitted_at':'2026-01-01T00:00:01+00:00','status':'queued','selection':{'research':'baseline'}}
            queue.save(active);queue.save(queued)
            tests.create_run.return_value=Mock(run={'id':'new'})
            with patch('test_requests.threading.Thread'):queue.tick()
            self.assertEqual(queue.view(queued['id'])['run_id'],'new')
            self.assertEqual(queue.view(active['id'])['status'],'running')

    def test_research_submission_is_idempotent_and_cancellation_is_scoped(self):
        with tempfile.TemporaryDirectory() as folder:
            tests=SimpleNamespace(runner=SimpleNamespace(folder=Path(folder)),lock=threading.RLock(),stop_run=Mock(return_value=True))
            queue=TestRequests(tests)
            ident='00000000-0000-0000-0000-000000000001'
            with patch('research_jobs.profiles',return_value={'baseline':{}}),patch.object(queue,'start'):
                self.assertEqual(queue.submit_research('baseline',ident),queue.submit_research('baseline',ident))
                with self.assertRaises(ValueError):queue.submit_research('unknown',ident)
            self.assertTrue(queue.cancel(ident));tests.stop_run.assert_not_called()
            self.assertEqual(queue.view(ident)['status'],'stopped')

    def test_new_runners_share_publication_lock_without_recovering_active_history(self):
        service=LiveDistributedTests.__new__(LiveDistributedTests)
        service.lock=threading.RLock();service.reruns={}
        with tempfile.TemporaryDirectory() as folder:
            service.runner=SimpleNamespace(root=Path(folder),folder=Path(folder),running=True,
                publication_lock=threading.Lock(),run={'id':'old','selection':{'suites':['map']}})
            runner=Mock(run={'id':'new'})
            with patch('distributed.DistributedTests',return_value=runner) as factory:
                self.assertIs(service.create_run(selection={'suites':['monitor']}),runner)
                factory.assert_called_once_with(Path(folder),Path(folder),recover=False)
            self.assertIs(runner.publication_lock,service.runner.publication_lock)
            self.assertIs(service.reruns['new'],runner)

class ResearchProjectionTests(unittest.TestCase):
    def test_private_state_exposes_only_batch_progress_and_operational_hosts(self):
        from research_jobs import ResearchRun
        service=ResearchRun.__new__(ResearchRun)
        service.lock=threading.RLock();service.run={};service.save=Mock()
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)
            (path/'state.json').write_text(json.dumps({'sources':{'qc':{'path':'/secret/sample.vcf'}},'jobs':{
                'private-dataset-qc-1':{'host':'worker','status':'passed','records':123,'sample':'PRIVATE','cpu_seconds':2.5,'elapsed_seconds':4,'reused':True},
                'private-dataset-qc-2':{'host':'worker','status':'waiting_capacity'}}}))
            service.read_progress(path)
        self.assertEqual(service.run['progress'],{'total':2,'completed':1,'failed':0})
        self.assertEqual(service.run['hosts'][0]['status'],'running')
        batch=service.run['hosts'][0]['jobs']['batch-0']
        self.assertEqual(batch['cpu_seconds'],2.5)
        self.assertEqual(batch['duration_seconds'],4)
        self.assertTrue(batch['reused'])
        self.assertEqual(service.run['hosts'][0]['jobs']['batch-1']['phase'],'waiting_for_capacity')
        for secret in ('private-dataset','/secret','PRIVATE','records'):self.assertNotIn(secret,json.dumps(service.run))

class HardwareAdmissionTests(unittest.TestCase):
    def test_hardware_run_does_not_block_ordinary_or_research_admission(self):
        service=LiveDistributedTests.__new__(LiveDistributedTests)
        service.lock=threading.RLock();service.reruns={}
        with tempfile.TemporaryDirectory() as folder:
            service.runner=SimpleNamespace(root=Path(folder),folder=Path(folder),running=True,
                publication_lock=threading.Lock(),run={'id':'hardware','selection':None})
            self.assertIsNone(service.create_run(selection={'suites':['gpu']}))
            runner=Mock(run={'id':'ordinary'})
            with patch('distributed.DistributedTests',return_value=runner):
                self.assertIs(service.create_run(selection={'suites':['monitor']}),runner)
            research=Mock(run={'id':'research'})
            with patch('research_jobs.ResearchRun',return_value=research):
                self.assertIs(service.create_run(selection={'research':'baseline'}),research)

class ProcessBudgetTests(unittest.TestCase):
    def test_process_headroom_blocks_launch_even_when_cpu_and_memory_fit(self):
        with tempfile.TemporaryDirectory() as directory:
            limits={'cpus':8,'memory_mb':8192,'pids':1024,'available_pids':100}
            lease=ResourceLease('test',1,512,directory=directory,limits=limits,pids=128)
            try:
                self.assertFalse(lease.try_acquire())
                limits['available_pids']=200
                self.assertTrue(lease.try_acquire())
            finally:lease.close()
