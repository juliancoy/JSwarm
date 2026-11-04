import threading
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch
from early_p2p import EarlyP2P

class EarlyP2PTests(TestCase):
    def test_network_starts_in_background_and_releases_reservation(self):
        entered,release=threading.Event(),threading.Event()
        hosts=[{'host':name,'jobs':{'p2p':{'status':'queued'}}} for name in ('a','b')]
        service=SimpleNamespace(run={'hosts':hosts,'force':True},root='.',folder='.',lock=threading.RLock(),save=Mock())
        def network(*args,**kwargs):
            self.assertTrue(kwargs['parallel'])
            entered.set()
            release.wait(2)
        service.network=network
        images=Mock(host_images={})
        with patch('early_p2p.SuiteCache') as cache:
            def fingerprints(run,*args,**kwargs):
                self.assertTrue(kwargs['force'])
                for host in run['hosts']:host['jobs']['p2p']['input_fingerprint']='verified'
            cache.return_value.apply.side_effect=fingerprints
            runner=EarlyP2P(service,hosts,images,True)
            try:
                self.assertTrue(entered.wait(2))
                self.assertFalse(runner.done.is_set())
                self.assertTrue(service.run['p2p_early'])
            finally:
                release.set()
                runner.join()
            self.assertTrue(runner.done.is_set())
            self.assertTrue(all(h['jobs']['p2p']['input_fingerprint']=='verified' for h in hosts))

    def test_network_failure_is_recorded_and_releases_reservation(self):
        host={'jobs':{'p2p':{'status':'queued'}}}
        service=SimpleNamespace(run={'hosts':[host]},lock=threading.RLock(),save=Mock(),network=Mock(side_effect=RuntimeError('browser failed')))
        runner=EarlyP2P(service,[host],Mock(),True)
        runner.join()
        self.assertTrue(runner.done.is_set())
        self.assertEqual(host['jobs']['p2p']['status'],'failed')
