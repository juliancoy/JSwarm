import collections
from pathlib import Path
import sys
import tempfile
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from launcher_agent import persist_job_event,persisted_job_events

class WorkerEventTests(unittest.TestCase):
    def test_buffer_eviction_and_restart_do_not_lose_source_events(self):
        with tempfile.TemporaryDirectory() as directory:
            job={'sequence':0,'events':collections.deque(maxlen=2)}
            for i in range(5):persist_job_event(directory,'run',job,{'kind':'log','line':str(i)})
            self.assertEqual(len(job['events']),2)
            replay=persisted_job_events(directory,'run',0)
            self.assertEqual([e['sequence'] for e in replay],[1,2,3,4,5])
            self.assertEqual([e['event']['line'] for e in replay],list(map(str,range(5))))
            self.assertEqual(len(persisted_job_events(directory,'run',3)),2)
            self.assertEqual(persisted_job_events(directory,'run',5),[])
            self.assertIsNone(persisted_job_events(directory,'another',0))
