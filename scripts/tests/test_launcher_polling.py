import asyncio
import base64
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest
import urllib.error
from unittest.mock import Mock,patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from launcher_peers import EnrollmentPoller


def invitation(peer='device-a',expires=None):
    claims={'aud':'launcher-enroll-v1','peer':peer,'room':'a'*64,'exp':expires or time.time()+300}
    payload=base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip('=')
    return {'url':'https://example.test','token':'header.'+payload+'.signature'}


class LauncherEnrollmentTests(unittest.TestCase):
    def test_unenrolled_launcher_waits_without_network_requests(self):
        with tempfile.TemporaryDirectory() as folder:
            poller=EnrollmentPoller(Path(folder)/'peers.json');poller.state=Mock()
            with patch('launcher_registration.register') as register:
                self.assertEqual(asyncio.run(poller.poll({})),{})
            register.assert_not_called();poller.state.assert_called_with('waiting-for-invitation')

    def test_temporary_registration_outage_uses_backoff(self):
        with tempfile.TemporaryDirectory() as folder:
            poller=EnrollmentPoller(Path(folder)/'peers.json');poller.state=Mock()
            poller.invitation.write_text(json.dumps(invitation()))
            error=urllib.error.HTTPError('https://example.test',503,'Unavailable',None,None)
            with patch('launcher_registration.register',side_effect=error) as register:
                asyncio.run(poller.poll({}));asyncio.run(poller.poll({}))
            register.assert_called_once();self.assertGreater(poller.next_attempt,time.monotonic())
            self.assertFalse(poller.blocked)

    def test_rejected_invitation_waits_for_replacement(self):
        with tempfile.TemporaryDirectory() as folder:
            poller=EnrollmentPoller(Path(folder)/'peers.json');poller.state=Mock()
            poller.invitation.write_text(json.dumps(invitation()))
            error=urllib.error.HTTPError('https://example.test',401,'Unauthorized',None,None)
            with patch('launcher_registration.register',side_effect=error) as register:
                asyncio.run(poller.poll({}));asyncio.run(poller.poll({}))
                self.assertEqual(register.call_count,1)
                poller.invitation.write_text(json.dumps(invitation(peer='replacement')))
                asyncio.run(poller.poll({}))
                self.assertEqual(register.call_count,2)
            self.assertTrue(poller.blocked)

    def test_expired_invitation_is_not_sent_to_server(self):
        with tempfile.TemporaryDirectory() as folder:
            poller=EnrollmentPoller(Path(folder)/'peers.json');poller.state=Mock()
            poller.invitation.write_text(json.dumps(invitation(expires=time.time()-60)))
            with patch('launcher_registration.register') as register:
                asyncio.run(poller.poll({}))
            register.assert_not_called();poller.state.assert_called_with('new-invitation-required')
