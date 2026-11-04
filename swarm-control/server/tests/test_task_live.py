import unittest
from unittest.mock import patch
from task_dashboard import TaskFeed

class LiveTaskFeedTests(unittest.TestCase):
    def test_clients_share_cache_and_only_real_changes_increment_revision(self):
        sample={'sampled_at':1,'tasks':[{'id':'work','status':'queued'}]}
        with patch('task_dashboard.snapshot',return_value=sample) as snapshot,patch('task_dashboard.time.monotonic',return_value=10) as clock:
            feed=TaskFeed();revision,data=feed.get();self.assertEqual(revision,1)
            feed.get();self.assertEqual(snapshot.call_count,1)
            clock.return_value=13;snapshot.return_value={**sample,'sampled_at':2}
            self.assertEqual(feed.get()[0],1)
            clock.return_value=16;snapshot.return_value={'sampled_at':3,'tasks':[{'id':'work','status':'running'}]}
            self.assertEqual(feed.get()[0],2)
