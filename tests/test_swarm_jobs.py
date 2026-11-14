import asyncio
import base64
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock,AsyncMock
sys.path.insert(0,str((Path(__file__).resolve().parents[2] if Path(__file__).resolve().parents[1].name in ('HarborGenetics', 'JSwarm') else Path(__file__).resolve().parents[1])/'scripts'))
from launcher_agent import worker_policy,Agent
from launcher_peers import JobGateway

class PolicyTests(unittest.TestCase):
    def test_battery_auto_ac_and_user_override(self):
        with tempfile.TemporaryDirectory() as t:
            p=Path(t);power=p/'power';power.mkdir();bat=power/'BAT0';bat.mkdir()
            (bat/'type').write_text('Battery');(bat/'status').write_text('Discharging');(bat/'capacity').write_text('42')
            cfg=p/'peers.json'
            self.assertFalse(worker_policy(cfg,power)['accepting_jobs'])
            ac=power/'AC';ac.mkdir();(ac/'type').write_text('Mains');(ac/'online').write_text('1')
            self.assertTrue(worker_policy(cfg,power)['accepting_jobs'])
            (p/'worker-policy.json').write_text('{"mode":"paused"}')
            self.assertFalse(worker_policy(cfg,power)['accepting_jobs'])
            (p/'worker-policy.json').write_text('{"mode":"always"}');(ac/'online').write_text('0')
            self.assertTrue(worker_policy(cfg,power)['accepting_jobs'])
    def test_paused_agent_rejects_new_work_before_docker(self):
        with tempfile.TemporaryDirectory() as t:
            p=Path(t);(p/'worker-policy.json').write_text('{"mode":"paused"}')
            agent=Agent.__new__(Agent);agent.config=p/'peers.json'
            for op,args in [('job.start',{}),('source.stage',{}),('p2p.start',{}),('image.pull',{}),('job.feed',{'tasks':['x']})]:
                with self.assertRaisesRegex(RuntimeError,'paused'):agent.dispatch(op,args)
            self.assertFalse(agent.dispatch('worker.policy',{})['accepting_jobs'])

class TransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_fragmented_roundtrip_is_binary_exact(self):
        with tempfile.TemporaryDirectory() as t:
            p=Path(t)/'peers.json';p.write_text(json.dumps({'internet_nodes':{'worker':'remote'}}))
            agent=Mock(config=p);g=JobGateway(agent);ident='a'*32
            future=asyncio.get_running_loop().create_future();g.pending[('remote',ident)]=future
            data=b'\x00\xff'*20000
            for seq,off in enumerate(range(0,len(data),12000)):
                g.receive('remote',{'id':ident,'reply':True,'seq':seq,'end':off+12000>=len(data),'data':base64.b64encode(data[off:off+12000]).decode()})
            self.assertEqual(await future,data);self.assertFalse(g.parts)
    async def test_unenrolled_and_unsolicited_messages_rejected(self):
        with tempfile.TemporaryDirectory() as t:
            p=Path(t)/'peers.json';p.write_text(json.dumps({'internet_nodes':{'worker':'remote'}}))
            g=JobGateway(Mock(config=p));m={'id':'a'*32,'reply':True,'seq':0,'end':True,'data':'AA=='}
            with self.assertRaisesRegex(ValueError,'Unenrolled'):g.receive('stranger',m)
            with self.assertRaisesRegex(ValueError,'Unsolicited'):g.receive('remote',m)
    async def test_invalid_sequence_rejected(self):
        with tempfile.TemporaryDirectory() as t:
            p=Path(t)/'peers.json';p.write_text(json.dumps({'internet_nodes':{'worker':'remote'}}))
            g=JobGateway(Mock(config=p))
            with self.assertRaises(ValueError):g.receive('remote',{'id':'a'*32,'reply':False,'seq':9,'end':False,'data':'AA=='})
    async def test_disconnect_does_not_silently_accept_request(self):
        with tempfile.TemporaryDirectory() as t:
            p=Path(t)/'peers.json';p.write_text(json.dumps({'internet_nodes':{'worker':'remote'}}))
            g=JobGateway(Mock(config=p))
            with self.assertRaisesRegex(RuntimeError,'not connected'):await g.remote('worker',b'encrypted')
            self.assertFalse(g.pending)

class AuthenticationTests(unittest.IsolatedAsyncioTestCase):
    async def test_replay_wrong_key_and_wrong_target_never_dispatch(self):
        import secrets,time,threading
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        with tempfile.TemporaryDirectory() as t:
            key=secrets.token_bytes(32);p=Path(t)/'peers.json'
            p.write_text(json.dumps({'name':'master','peers':{'client':'10.0.0.2'},'key':key.hex()}))
            agent=Agent.__new__(Agent);agent.config=p;agent.lock=threading.RLock();agent.nonce_lock=threading.Lock();agent.nonces={};agent.dispatch=Mock(return_value={'accepted':True})
            value={'from':'client','to':'master','nonce':'a'*32,'time':time.time(),'op':'tests.request','args':{'suites':['monitor']}}
            def seal(v,k=key):
                n=secrets.token_bytes(12);return n+AESGCM(k).encrypt(n,json.dumps(v).encode(),b'lifetech-agent.v1')
            raw=seal(value);reply=await agent.process(raw)
            self.assertTrue(json.loads(AESGCM(key).decrypt(reply[:12],reply[12:],b'lifetech-agent.v1'))['ok'])
            for bad in (raw,seal(value,secrets.token_bytes(32)),seal({**value,'to':'other','nonce':'b'*32}),seal({**value,'time':time.time()-600,'nonce':'c'*32})):
                with self.assertRaises(ValueError):await agent.process(bad)
            agent.dispatch.assert_called_once_with('tests.request',{'suites':['monitor']})

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding,PublicFormat

from launcher_agent import verify_controller

class SignatureTests(unittest.TestCase):
    def test_fleet_secret_does_not_authorize_execution(self):
        key=Ed25519PrivateKey.generate()
        config={'master':'master','controller_public_key':key.public_key().public_bytes(Encoding.Raw,PublicFormat.Raw).hex()}
        value={'from':'master','to':'worker','op':'job.start','args':{'manifest':'trusted'},'nonce':'a','time':100}
        with self.assertRaisesRegex(ValueError,'signature'):verify_controller(value,config)
        value['controller_signature']=base64.b64encode(key.sign(json.dumps(value,sort_keys=True,separators=(',',':')).encode())).decode()
        verify_controller(value,config)
        for field,replacement in [('from','peer'),('to','other'),('op','p2p.start'),('args',{'command':'malicious'}),('nonce','b'),('time',101)]:
            with self.subTest(field=field),self.assertRaises(ValueError):verify_controller({**value,field:replacement},config)
        with self.assertRaises(ValueError):verify_controller(value,{'master':'master'})
    def test_unknown_operations_fail_closed_but_peers_can_request_tests(self):
        verify_controller({'op':'tests.request'},{})
        for op in ['job.start','job.feed','source.stage','upload.begin','p2p.start','future.operation']:
            with self.subTest(op=op),self.assertRaises(ValueError):verify_controller({'op':op},{})


if __name__=='__main__':unittest.main()

class OwnerBrowserTests(unittest.TestCase):
    def test_loopback_request_cannot_mint_owner_access_without_private_launch(self):
        from launcher_peers import owner_launcher_url
        from urllib.parse import urlsplit,parse_qs
        with tempfile.TemporaryDirectory() as t:
            p=Path(t)/'peers.json';p.write_text(json.dumps({'name':'compai.local','master_api':True}))
            gateway=JobGateway(Mock(config=p))
            self.assertFalse(gateway.owner_session_valid({}))
            self.assertFalse(gateway.owner_session_valid({'X-Launcher-Owner':'forged'*8}))
            url=owner_launcher_url(p);token=parse_qs(urlsplit(url).fragment)['owner'][0]
            self.assertNotIn(token,(p.parent/'owner-browser-bootstrap.json').read_text())
            self.assertEqual((p.parent/'owner-browser-bootstrap.json').stat().st_mode&0o777,0o600)
            with self.assertRaises(ValueError):gateway.unlock_owner({'token':'forged'*8})
            session=gateway.unlock_owner({'token':token})
            self.assertTrue(gateway.owner_session_valid({'X-Launcher-Owner':session}))
            with self.assertRaises(OSError):gateway.unlock_owner({'token':token})
            gateway.owner_sessions.clear()
            self.assertFalse(gateway.owner_session_valid({'X-Launcher-Owner':session}))
    def test_worker_launch_cannot_unlock_owner_capabilities(self):
        from launcher_peers import owner_launcher_url
        with tempfile.TemporaryDirectory() as t:
            p=Path(t)/'peers.json';p.write_text(json.dumps({'name':'laptop','master_api':False}))
            self.assertNotIn('#owner=',owner_launcher_url(p))
            with self.assertRaises(ValueError):JobGateway(Mock(config=p)).unlock_owner({'token':'forged'*8})
