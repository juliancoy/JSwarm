import base64
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import apk_delivery as deploy

class ArtifactTests(unittest.TestCase):
    def test_only_fixed_bounded_apk_and_no_symlinks(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'app.apk'
            with patch.object(deploy,'ARTIFACT',path):
                with self.assertRaises(ValueError):deploy.artifact()
                path.write_bytes(b'not an apk')
                with self.assertRaises(ValueError):deploy.artifact()
                path.write_bytes(b'PK-fixture')
                data,manifest=deploy.artifact()
                self.assertEqual(manifest['sha256'],hashlib.sha256(data).hexdigest())
                link=Path(folder)/'link.apk';link.symlink_to(path)
                with patch.object(deploy,'ARTIFACT',link),self.assertRaises(ValueError):deploy.artifact()
    def test_chunk_bounds_receipts_and_narrow_operation_allowlist(self):
        data=b'PK'+b'x'*20000
        manifest={'size':len(data),'sha256':hashlib.sha256(data).hexdigest()}
        part=deploy.request(data,manifest,{'id':1,'op':'chunk','offset':0})
        self.assertEqual(base64.b64decode(part['data']),data[:deploy.CHUNK])
        for message in [{'id':1,'op':'chunk','offset':-1},{'id':1,'op':'chunk','offset':True},{'id':1,'op':'chunk','offset':len(data)},{'id':1,'op':'shell'},{'id':1,'op':'manifest','path':'/etc/passwd'},{'id':True,'op':'ping'},{'id':1,'op':'received','sha256':'bad'}]:
            with self.subTest(message=message),self.assertRaises(ValueError):deploy.request(data,manifest,message)
        self.assertTrue(deploy.request(data,manifest,{'id':1,'op':'received','sha256':manifest['sha256']})['verified'])

class RtcTests(unittest.IsolatedAsyncioTestCase):
    async def test_actual_datachannel_transfer_digest_and_revoked_session(self):
        import asyncio
        from aiortc import RTCPeerConnection,RTCConfiguration
        # Distributed containers have only loopback. Exercise real UDP/DTLS/SCTP
        # between these peers without requiring an external interface or STUN.
        addresses=patch('aioice.ice.get_host_addresses',return_value=['127.0.0.1'])
        addresses.start()
        self.addCleanup(addresses.stop)
        peer=RTCPeerConnection(RTCConfiguration(iceServers=[]))
        manager=deploy.Deployment();channel=peer.createDataChannel('swarm-control-test.v1')
        opened=asyncio.Event();responses=asyncio.Queue()
        channel.on('open',opened.set)
        channel.on('message',lambda raw:responses.put_nowait(json.loads(raw)))
        valid=True
        try:
            await peer.setLocalDescription(await peer.createOffer())
            with patch.object(deploy,'artifact',return_value=(b'PK-test',{'size':7,'sha256':hashlib.sha256(b'PK-test').hexdigest(),'chunkSize':deploy.CHUNK})):
                answer=await asyncio.to_thread(manager.offer,{'type':'offer','sdp':peer.localDescription.sdp},lambda:valid)
            from aiortc import RTCSessionDescription
            await peer.setRemoteDescription(RTCSessionDescription(answer['sdp'],'answer'))
            await asyncio.wait_for(opened.wait(),15)
            channel.send(json.dumps({'id':1,'op':'chunk','offset':0}))
            result=await asyncio.wait_for(responses.get(),5)
            self.assertEqual(base64.b64decode(result['data']),b'PK-test')
            channel.send(json.dumps({'id':2,'op':'received','sha256':hashlib.sha256(b'PK-test').hexdigest()}))
            self.assertTrue((await asyncio.wait_for(responses.get(),5))['verified'])
            valid=False
            channel.send(json.dumps({'id':3,'op':'manifest'}))
            with self.assertRaises(asyncio.TimeoutError):await asyncio.wait_for(responses.get(),.2)
        finally:
            await peer.close()
            for ident in list(manager.peers):await asyncio.wrap_future(asyncio.run_coroutine_threadsafe(manager.close(ident),manager.loop))
            manager.loop.call_soon_threadsafe(manager.loop.stop)
