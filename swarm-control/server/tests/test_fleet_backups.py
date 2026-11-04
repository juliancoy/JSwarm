import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock,patch
from master_api import APIError,MasterAPI,tool_definitions
from fleet_backups import FleetBackups


class FleetMasterTests(unittest.TestCase):
    def test_authenticated_api_surface_uses_one_service_and_rejects_paths(self):
        api=MasterAPI(Mock(lock=threading.RLock()));api._backups=Mock()
        api._backups.start.return_value={'accepted':True,'status':'packing'}
        api._backups.view.return_value={'status':'replicating'}
        self.assertEqual(api.rest('POST','/api/v1/master/backups',{})[1]['status'],'packing')
        self.assertEqual(api.rest('GET','/api/v1/master/backups')[1]['status'],'replicating')
        with self.assertRaises(APIError):api.rest('POST','/api/v1/master/backups',{'path':'/etc'})
        self.assertFalse(next(t for t in tool_definitions() if t['name']=='create_backups')['annotations']['readOnlyHint'])
        self.assertIn('/api/v1/master/backups',api.openapi()['paths'])
    def test_coordinator_keeps_verified_package_on_its_recovery_drive(self):
        with tempfile.TemporaryDirectory() as temporary:
            service=FleetBackups(Mock(),temporary)
            service.state={'status':'replicating','devices':{'self.local':{}}}
            manifest={'snapshot':'20261024T180859Z','parts':[{'sha256':'a'*64,'size':100}]}
            service.device({'host':'self.local','local':True},Path('/mnt/recovery/generation'),manifest)
            result=service.view()['devices']['self.local']
            self.assertEqual(result['status'],'verified')
            self.assertEqual(result['receipt']['path'],'/mnt/recovery/generation')
            self.assertEqual(result['bytesStored'],100)

    def test_upload_rate_bounds_authenticated_request_frequency(self):
        from agent_client import AgentClient
        with tempfile.TemporaryDirectory() as temporary:
            path=Path(temporary)/'ciphertext';path.write_bytes(b'x'*1024)
            client=object.__new__(AgentClient)
            def call(op,args,**kwargs):
                return {'upload':'token'} if op=='upload.begin' else {'offset':1024} if op=='upload.chunk' else {'verified':True}
            client.call=call
            with patch('agent_client.time.monotonic',return_value=0),patch('agent_client.time.sleep') as sleep:
                self.assertEqual(client.upload(path,max_bytes_per_second=1024),'token')
                sleep.assert_called_once_with(1)
            with self.assertRaises(ValueError):client.upload(path,max_bytes_per_second=0)

    def test_coordinator_restart_marks_unfinished_transfer_for_resume(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder=Path(temporary);(folder/'latest.json').write_text(json.dumps({'status':'replicating','devices':{'a.local':{'bytesStored':100}}}))
            service=FleetBackups(Mock(),folder)
            self.assertEqual(service.view()['status'],'interrupted')
            self.assertEqual(service.view()['devices']['a.local']['bytesStored'],100)
            copy=service.view();copy['devices'].clear();self.assertEqual(len(service.view()['devices']),1)

if __name__=='__main__':unittest.main()
