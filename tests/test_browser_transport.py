import json
import base64
import hashlib
import subprocess
import io
import tarfile
from pathlib import Path
import sys
import unittest
import tempfile
import threading
from unittest.mock import Mock,MagicMock,patch
sys.path.insert(0,str((Path(__file__).resolve().parents[2] if Path(__file__).resolve().parents[1].name in ('GenomeHarbor', 'JSwarm') else Path(__file__).resolve().parents[1])/'scripts'))
from launcher_agent import browser_request,Agent

class BrowserTransportTests(unittest.TestCase):
    def test_cleanup_accepts_concurrent_removal_and_missing_container(self):
        from docker.errors import APIError, NotFound
        response=Mock(status_code=409)
        for error in (NotFound('gone'), APIError('conflict',response=response,
                     explanation='removal of container '+64*'a'+' is already in progress')):
            with self.subTest(error=type(error).__name__):
                container=Mock();container.remove.side_effect=error
                Agent.remove_container(container)
                container.remove.assert_called_once_with(force=True)

    def test_cleanup_preserves_unrelated_docker_errors(self):
        from docker.errors import APIError
        for status,explanation in ((409,'container is paused'),(500,'daemon unavailable')):
            with self.subTest(status=status):
                error=APIError('failure',response=Mock(status_code=status),explanation=explanation)
                container=Mock();container.remove.side_effect=error
                with self.assertRaises(APIError):Agent.remove_container(container)

    def test_worker_module_imports_as_a_standalone_bundle(self):
        with tempfile.TemporaryDirectory() as temporary:
            worker=Path(temporary)/'worker.py'
            worker.write_bytes(((Path(__file__).resolve().parents[2] if Path(__file__).resolve().parents[1].name in ('GenomeHarbor', 'JSwarm') else Path(__file__).resolve().parents[1])/'scripts/launcher_agent.py').read_bytes())
            subprocess.run([sys.executable,'-I','-c','import runpy,sys; runpy.run_path(sys.argv[1])',str(worker)],check=True,capture_output=True)

    def test_upload_verifies_bytes_without_python_311_file_digest(self):
        with tempfile.TemporaryDirectory() as temporary:
            agent=Agent.__new__(Agent);agent.root=Path(temporary)
            agent.lock=threading.RLock();agent.uploads={};agent.busy=Mock()
            raw=b'fixture bytes'*100000
            with patch.object(hashlib,'file_digest',None,create=True):
                token=agent.upload('upload.begin',{'size':len(raw),'sha256':hashlib.sha256(raw).hexdigest()})['upload']
                for offset in range(0,len(raw),64*1024):
                    agent.upload('upload.chunk',{'upload':token,'offset':offset,'data':base64.b64encode(raw[offset:offset+64*1024]).decode()})
                self.assertTrue(agent.upload('upload.finish',{'upload':token})['verified'])
                agent.uploads[token]['path'].write_bytes(b'corrupted bytes')
                with self.assertRaisesRegex(ValueError,'checksum mismatch'):
                    agent.upload('upload.finish',{'upload':token})

    def test_requests_use_the_owned_container_namespace_without_a_shell(self):
        container=Mock();container.exec_run.return_value=(0,b'{"value":{"ready":true}}')
        data={'script':'return "$(do-not-execute)";','args':['x'*(256*1024)]}
        self.assertEqual(browser_request(container,'/session/owned/execute/sync',data,'POST'),{'value':{'ready':True}})
        args=container.exec_run.call_args
        self.assertIsInstance(args.args[0],list)
        self.assertEqual(args.args[0][:2],['/usr/bin/python3','-c'])
        self.assertTrue(args.args[0][-1].startswith('/tmp/webdriver-'))
        self.assertTrue(all(len(arg)<4096 for arg in args.args[0]))
        staged=container.put_archive.call_args
        self.assertEqual(staged.args[0],'/tmp')
        with tarfile.open(fileobj=io.BytesIO(staged.args[1])) as archive:
            member=archive.getmembers()[0]
            self.assertEqual(member.mode,0o600);self.assertEqual(member.uid,1200)
            self.assertEqual('/tmp/'+member.name,args.args[0][-1])
            self.assertEqual(json.load(archive.extractfile(member)),{'path':'/session/owned/execute/sync','data':data,'method':'POST'})
        self.assertEqual(args.kwargs['user'],'1200:1201')

    def test_exec_failure_is_redacted_and_oversized_responses_fail(self):
        for result in ((1,b'private diagnostic'),(0,b'x'*(4*1024*1024+1))):
            container=Mock();container.exec_run.return_value=result
            with self.assertRaisesRegex(RuntimeError,'Owned browser WebDriver request failed'):
                browser_request(container,'/status',None,'GET')

    def test_private_agent_umask_does_not_make_public_browser_fixture_unreadable(self):
        with tempfile.TemporaryDirectory() as temporary:
            agent=Agent.__new__(Agent);agent.root=Path(temporary)
            agent.browsers=set();agent.browser_timers={};agent.lock=threading.RLock();agent.busy=Mock()
            def extract(upload,folder):
                folder.mkdir(mode=0o700)
                for name in ('remote.py','peer.js'):(folder/name).write_text('public fixture')
            agent.extract=extract
            client=MagicMock();client.__enter__.return_value=client;agent.client=lambda:client
            image='sha256:'+64*'a';client.images.get.return_value.id=image
            client.containers.run.return_value.id='owned-container'
            ident='20261024T221500Z-1234abcd'
            with patch('launcher_agent.threading.Timer'), patch('launcher_agent.ResourceLease') as lease:
                lease.return_value.try_acquire.return_value=True
                agent.p2p_start({'id':ident,'image':image,'upload':'fixture'})
            folder=agent.folder(ident+'-p2p')
            self.assertEqual(folder.stat().st_mode & 0o777,0o755)
            self.assertEqual((folder/'remote.py').read_text(),'public fixture')
            self.assertEqual(client.containers.run.call_args.kwargs['user'],'1200:1201')
