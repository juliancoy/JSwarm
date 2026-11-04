import copy
import json
import unittest
from unittest.mock import patch
from cpu import aggregate, CpuMonitor, ResourceSampler


def snapshot(at, busy, idle, iowait=0, steal=0, boot='boot'):
    values=[busy,0,0,idle,iowait,0,0,steal]
    return {'monotonic':at,'sampled_at':at,'boot_id':boot,'cpu_counters':{'cpu':values,'cpu0':values},
            'cpu_error':None,'network_bytes':int(at*1000000),'disk_counters':{'sda':int(at*500)},
            'ram_total_bytes':100,'ram_percent':20}


class CpuTests(unittest.TestCase):
    def test_total_is_capacity_weighted_and_excludes_unavailable_hosts(self):
        self.assertEqual(aggregate([{'percent':100,'cpus':2,'status':'live'},
                                   {'percent':0,'cpus':6,'status':'live'},
                                   {'percent':99,'cpus':32,'status':'stale'}]),25)
        self.assertIsNone(aggregate([]))
        self.assertIsNone(aggregate([{'percent':float('nan'),'cpus':8,'status':'live'}]))

    def test_entire_interval_includes_bursts_and_separates_wait_and_steal(self):
        samples=iter([snapshot(1,0,100),snapshot(6,200,300,50,50)])
        sampler=ResourceSampler(lambda:copy.deepcopy(next(samples)))
        self.assertEqual(sampler.sample()['status'],'warming')
        value=sampler.sample()
        self.assertEqual(value['window_seconds'],5)
        self.assertEqual(value['percent'],40)
        self.assertEqual(value['cpu_iowait_percent'],10)
        self.assertEqual(value['cpu_steal_percent'],10)
        self.assertEqual(value['logical_cpus'][0]['percent'],40)
        self.assertEqual(value['network_mbps'],8)
        self.assertEqual(value['disk_busy_percent'],50)

    def test_counter_reset_reboot_and_long_gap_never_report_idle(self):
        for second in [snapshot(6,0,0),snapshot(6,10,200,boot='new'),snapshot(60,10,200)]:
            with self.subTest(second=second):
                samples=iter([snapshot(1,100,100),second]);sampler=ResourceSampler(lambda:copy.deepcopy(next(samples)))
                sampler.sample();result=sampler.sample()
                self.assertEqual(result['status'],'warming');self.assertIsNone(result['percent'])

    def test_permission_failure_is_not_zero(self):
        value=snapshot(1,0,0);value.update(cpu_counters=None,cpu_error='permission_denied')
        result=ResourceSampler(lambda:value).sample()
        self.assertEqual(result['status'],'permission_denied');self.assertIsNone(result['percent'])

    def test_old_samples_do_not_appear_live(self):
        monitor=CpuMonitor();monitor.hosts=[{'host':'a','percent':50,'cpus':8,'status':'live','sampled_at':1}]
        with patch('cpu.time.time',return_value=20):view=monitor.view()
        self.assertIsNone(view['total_percent']);self.assertEqual(view['live_hosts'],0)
        self.assertEqual(view['hosts'][0]['status'],'stale')

    def test_test_active_average_does_not_include_idle_hosts_or_recorded_runs(self):
        reports={'running':True,'run':{'hosts':[{'host':'a','jobs':{'test':{'status':'running','phase':'test','cpus':1}}}]}}
        monitor=CpuMonitor(activity=lambda:reports)
        monitor.hosts=[{'host':'a','percent':80,'cpus':2,'status':'live','sampled_at':10},
                       {'host':'b','percent':0,'cpus':6,'status':'live','sampled_at':10}]
        with patch('cpu.time.time',return_value=11):
            view=monitor.view();self.assertEqual(view['total_percent'],20);self.assertEqual(view['active_test_percent'],80)
            self.assertEqual(view['measured_logical_cpus'],8)
            reports['running']=False;view=monitor.view();self.assertEqual(view['test_hosts'],0);self.assertIsNone(view['active_test_percent'])

    def test_local_snapshot_reports_real_counters_without_sleeping(self):
        values=ResourceSampler.capture()
        self.assertGreater(values['ram_total_bytes'],0)
        self.assertGreaterEqual(values['ram_percent'],0)
        self.assertIn('cpu',values['cpu_counters'])

    def test_activity_projection_omits_logs_and_keeps_reruns(self):
        import threading
        from types import SimpleNamespace
        from live_distributed import LiveDistributedTests
        service=LiveDistributedTests.__new__(LiveDistributedTests)
        service.lock=threading.RLock()
        host={'host':'a','status':'running','jobs':{'one':{'status':'running','phase':'test','cpus':1,'logs':['private output']},'finished':{'status':'passed','logs':['large output']}}}
        service.runner=SimpleNamespace(lock=threading.RLock(),running=True,run={'hosts':[host]})
        service.reruns={'rerun':SimpleNamespace(lock=threading.RLock(),running=True,run={'hosts':[host]})}
        result=service.cpu_activity()
        self.assertEqual(result['active_reruns'],['rerun'])
        self.assertNotIn('logs',result['run']['hosts'][0]['jobs']['one'])
        self.assertNotIn('finished',result['run']['hosts'][0]['jobs'])
        self.assertTrue(service.cpu_activity('rerun')['running'])
        service.runner.running=False
        self.assertEqual(service.cpu_activity()['run']['hosts'],[])
