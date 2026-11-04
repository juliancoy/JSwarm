import tempfile
import unittest
from datetime import datetime, timezone
from runtime_stats import record, report, summary, seconds


class RuntimeStatisticsTests(unittest.TestCase):
    def test_samples_are_idempotent_and_reused_results_do_not_bias_percentiles(self):
        with tempfile.TemporaryDirectory() as folder:
            def save(ident,status,duration,machine='a'):
                run={'id':ident,'finished':datetime.now(timezone.utc).isoformat(),'source_sha256':'source',
                     'hosts':[{'host':'worker','session':{'machine':machine},'jobs':{'test':{
                         'file':'tests/test_example.py','status':status,'duration_seconds':duration,
                         'timing_source':'worker_monotonic','phase_seconds':{'test':duration/2,'prepare':duration/2}}}}]}
                record(folder,run)
            save('1','passed',2);save('1','passed',2)
            save('2','passed',10);save('3','reused',100);save('4','failed',40)
            save('5','passed',60,'replacement-machine')
            groups=report(folder)['statistics'];a=next(g for g in groups if g['machine']=='a')
            self.assertEqual(a['observations'],4)
            self.assertEqual(a['reused'],1);self.assertEqual(a['failed'],1)
            self.assertEqual(a['total_seconds'],{'samples':2,'min':2,'median':6,'p95':10,'max':10})
            self.assertEqual(a['test_seconds']['median'],3)
            self.assertEqual(len(groups),2)
            self.assertEqual(report(folder,host='missing')['statistics'],[])

    def test_retention_and_absent_timings_are_not_zero_samples(self):
        with tempfile.TemporaryDirectory() as folder:
            run={'id':'old','finished':'2000-01-01T00:00:00+00:00','hosts':[{'host':'a','jobs':{'test':{'status':'passed','duration_seconds':1}}}]}
            record(folder,run)
            self.assertEqual(report(folder)['statistics'],[])
            run.update(id='new',finished=datetime.now(timezone.utc).isoformat())
            run['hosts'][0]['jobs']['test']={'status':'passed'}
            record(folder,run)
            self.assertEqual(report(folder)['statistics'][0]['test_seconds'],{'samples':0})

    def test_percentile_and_queue_timing(self):
        self.assertEqual(summary(list(range(1,21)))['p95'],19)
        self.assertEqual(seconds('2026-01-01T00:00:00+00:00','2026-01-01T00:00:12+00:00'),12)
        self.assertIsNone(seconds(None,None))
