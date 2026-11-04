import tempfile
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock,patch
from mobile_worker import discover,MobileRun
from test_scope import normalize,select

class MobileTests(TestCase):
    def test_adb_discovery_reports_physical_pixel_and_browser_readiness(self):
        with patch('mobile_worker.adb',side_effect=['List of devices attached\nabc device usb:1-2 model:Pixel_3\nother device model:Other\n','12','adreno','@chrome_devtools_remote']):
            devices=discover()['devices']
        self.assertEqual(len(devices),1)
        self.assertEqual(devices[0]['serial'],'abc')
        self.assertTrue(devices[0]['connected'])
        self.assertTrue(devices[0]['browser_ready'])
        self.assertEqual(devices[0]['slots'],1)
        self.assertFalse(devices[0]['gpu']['compute_verified'])

    def test_mobile_only_selection_does_not_dispatch_docker_test_jobs(self):
        selection=normalize(suites=['mobile'])
        self.assertEqual(select(['test'],{'test':{'base':'monitor'}},selection),[])

    def test_missing_requested_phone_fails_without_claiming_pass(self):
        with tempfile.TemporaryDirectory() as folder,patch('mobile_worker.discover',return_value={'devices':[]}),patch('mobile_worker.STATE',Path(folder)/'status.json'):
            service=SimpleNamespace(lock=threading.RLock(),save=Mock(),run={'id':'run','hosts':[]})
            mobile=MobileRun(service,required=True);mobile.join()
            self.assertEqual(mobile.host['jobs']['mobile']['status'],'failed')
            self.assertIn('not attached',mobile.host['error'])

    def test_optional_phone_absence_does_not_create_fake_worker(self):
        with patch('mobile_worker.discover',return_value={'devices':[]}):
            mobile=MobileRun(Mock())
            self.assertIsNone(mobile.host)

    def test_mobile_only_run_publishes_sources_without_requiring_docker_agents(self):
        from distributed import DistributedTests
        service=SimpleNamespace(root=Path('/repo'),folder=Path('/reports'),lock=threading.RLock(),publication_lock=threading.Lock(),save=Mock(),running=True,
            run={'id':'run','hosts':[],'selection':{'paths':[],'suites':['mobile']}})
        phone={'host':'phone','status':'passed','jobs':{'mobile':{'status':'passed'}}}
        with patch('github_sources.publish',return_value={}) as publish,patch('github_sources.source_digest',return_value='digest'),patch('mobile_worker.MobileRun',return_value=SimpleNamespace(host=phone,join=Mock())):
            DistributedTests.execute(service)
        self.assertEqual(service.run['status'],'passed')
        self.assertEqual(len(publish.call_args.args),4)
        self.assertFalse(service.running)
