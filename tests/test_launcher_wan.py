import base64
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest
sys.path.insert(0,str((Path(__file__).resolve().parents[2] if Path(__file__).resolve().parents[1].name in ('GenomeHarbor', 'JSwarm') else Path(__file__).resolve().parents[1])/'scripts'))
from launcher_registration import queue_invitation

class WANEnrollmentTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.config=Path(self.temp.name)/'peers.json'
        self.original={'name':'laptop','key':'private-fleet-key','peers':{'master':'10.0.0.1'},'internet_nodes':{'master':'peer-id'}}
        self.config.write_text(json.dumps(self.original))
    def invitation(self,**claims):
        payload={'aud':'launcher-enroll-v1','peer':'device-id','room':'room-id','exp':time.time()+600,**claims}
        token='header.'+base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip('=')+'.signature'
        return {'url':'https://example.org','token':token,'peer':'device-id','expires':payload['exp'],'registrationUrl':'https://example.org/launchers/register'}
    def test_issued_invitation_is_private_and_does_not_replace_fleet_settings(self):
        value=self.invitation();queue_invitation({'invitations':[value]},self.config)
        path=self.config.parent/'invitation.json'
        self.assertEqual(path.stat().st_mode&0o777,0o600)
        self.assertEqual(json.loads(path.read_text()),{**{k:value[k] for k in ('url','token')},'renew':True})
        self.assertEqual(json.loads(self.config.read_text()),self.original)
    def test_reject_expired_wrong_audience_multiple_devices_and_non_https(self):
        for value in [self.invitation(exp=0),self.invitation(aud='other'),{'invitations':[self.invitation(),self.invitation()]},{**self.invitation(),'url':'http://example.org'},{**self.invitation(),'key':'injected'}, {'url':'https://example.org','token':'not-a-token'}]:
            with self.subTest(value_type=type(value).__name__),self.assertRaises(ValueError):queue_invitation(value,self.config)
        self.assertFalse((self.config.parent/'invitation.json').exists())
    def test_unprovisioned_device_cannot_use_invitation_to_grant_fleet_execution(self):
        self.config.write_text('{}')
        with self.assertRaisesRegex(ValueError,'trusted swarm provisioning'):queue_invitation(self.invitation(),self.config)
    def test_oversized_invitation_does_not_write(self):
        with self.assertRaises(ValueError):queue_invitation({'url':'https://example.org','token':'x'*17000},self.config)

class WANRenewalTests(unittest.IsolatedAsyncioTestCase):
    async def test_explicit_renewal_registers_existing_identity_and_removes_used_invitation(self):
        from unittest.mock import patch
        from launcher_peers import EnrollmentPoller
        with tempfile.TemporaryDirectory() as t:
            path=Path(t)/'peers.json'
            config={'internet':{'peer':'device','room':'fleet','pending':False}}
            path.write_text(json.dumps(config))
            claims={'peer':'device','room':'fleet','aud':'launcher-enroll-v1','exp':time.time()+600}
            token='header.'+base64.urlsafe_b64encode(json.dumps(claims).encode()).decode()+'.signature'
            invitation=path.parent/'invitation.json'
            invitation.write_text(json.dumps({'url':'https://example.org','token':token,'renew':True}))
            poller=EnrollmentPoller(path)
            with patch('launcher_registration.register') as register,patch.object(poller,'state'):
                await poller.poll(config)
                register.assert_called_once_with(invitation,path)
            self.assertFalse(invitation.exists())
