import asyncio
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from launcher_agent import LauncherTelemetry, InterfaceSampler, DiskSampler, GpuSampler, Agent, VERSION
from launcher_peers import JobGateway


class TelemetryTests(unittest.TestCase):
    def telemetry(self):
        return LauncherTelemetry(SimpleNamespace())

    def cpu(self, at):
        return {'status':'live','sampled_at':at,'percent':25,'measurement_version':2,'cpus':4}

    def test_reconnect_prioritizes_latest_then_replays_and_deduplicates(self):
        source=self.telemetry();sink=self.telemetry();now=time.time()
        for i in range(70):source.record('cpu',self.cpu(now-290+i*4),now-290+i*4)
        batch=source.pending(set())
        self.assertEqual(batch['samples'][0]['sequence'],70)
        acknowledged=set()
        while (batch:=source.pending(acknowledged))['samples']:
            reply=sink.accept('worker',batch);acknowledged.update(reply['sequences'])
            sink.accept('worker',batch)  # Lost acknowledgement: safe to resend.
        fleet=sink.fleet({'worker'})
        self.assertEqual(len(fleet['events']),70)
        self.assertEqual(fleet['hosts'][0]['sampled_at'],now-14)
        self.assertEqual(sink.fleet({'worker'},fleet['revision'])['events'],[])
        self.assertEqual(sink.fleet({'other'})['hosts'],[])

    def test_fleet_history_is_paged_and_omits_repeated_per_cpu_arrays(self):
        source=self.telemetry();sink=self.telemetry();now=time.time()
        for _ in range(140):source.record('cpu',{**self.cpu(now),'logical_cpus':[{'id':0,'percent':25}]},now)
        acknowledged=set()
        while (batch:=source.pending(acknowledged))['samples']:
            reply=sink.accept('worker',batch);acknowledged.update(reply['sequences'])
        first=sink.fleet({'worker'});second=sink.fleet({'worker'},first['revision'])
        self.assertEqual(len(first['events']),128)
        self.assertEqual(len(second['events']),12)
        self.assertLess(first['revision'],second['revision'])
        self.assertIn('logical_cpus',first['hosts'][0])
        self.assertNotIn('logical_cpus',first['events'][0]['hosts'][0])
        self.assertEqual(sink.fleet({'worker'},second['revision'])['events'],[])

    def test_buffers_are_bounded_and_old_samples_expire(self):
        source=self.telemetry();now=time.time()
        for i in range(500):source.record('cpu',self.cpu(now),now)
        self.assertEqual(len(source.samples),256)
        with patch('launcher_agent.time.time',return_value=now+301):self.assertEqual(source.pending(set())['samples'],[])

    def test_bad_batch_does_not_partially_mutate_receiver(self):
        sink=self.telemetry();source=self.telemetry();now=time.time()
        source.record('cpu',self.cpu(now),now)
        batch=source.pending(set());batch['samples'].append({'sequence':2,'kind':'shell','sampled_at':now,'value':{}})
        with self.assertRaises(ValueError):sink.accept('worker',batch)
        self.assertEqual(sink.received,{})
        batch['samples'].pop();batch['samples'][0]['sampled_at']=float('nan')
        with self.assertRaises(ValueError):sink.accept('worker',batch)

    def test_global_transport_is_master_only_persistent_and_requires_controller_signature(self):
        from launcher_agent import UNPRIVILEGED_OPS
        self.assertNotIn('metrics.transport',UNPRIVILEGED_OPS)
        with tempfile.TemporaryDirectory() as folder:
            agent=Agent.__new__(Agent);agent.config=Path(folder)/'peers.json';agent.lock=threading.RLock()
            agent.config.write_text(json.dumps({'name':'master','master':'master'}))
            self.assertEqual(agent.telemetry_policy()['transport'],'tcp')
            saved=agent.dispatch('metrics.transport',{'transport':'webrtc'})
            restarted=Agent.__new__(Agent);restarted.config=agent.config
            self.assertEqual(restarted.telemetry_policy(),saved)
            self.assertEqual((Path(folder)/'telemetry-transport.json').stat().st_mode&0o777,0o600)
            with self.assertRaises(ValueError):agent.dispatch('metrics.transport',{'transport':'udp'})
            self.assertEqual(agent.telemetry_policy(),saved)
            agent.config.write_text(json.dumps({'name':'worker','master':'master'}))
            with self.assertRaises(ValueError):agent.dispatch('metrics.transport',{'transport':'tcp'})

    def test_metrics_read_never_collects_synchronously(self):
        agent=Agent.__new__(Agent);agent.telemetry=self.telemetry()
        with tempfile.TemporaryDirectory() as folder:
            agent.config=Path(folder)/'peers.json';agent.config.write_text(json.dumps({'name':'worker'}))
            agent.metrics_sampler=Mock()
            result=agent.dispatch('metrics',{})
            self.assertEqual(result['status'],'warming')
            agent.metrics_sampler.sample.assert_not_called()

    def test_slow_hardware_does_not_block_cpu_and_shutdown_stops_collectors(self):
        gate=threading.Event();cpu_ready=threading.Event()
        with tempfile.TemporaryDirectory() as folder:
            config=Path(folder)/'peers.json';config.write_text(json.dumps({'name':'worker'}))
            def cpu():
                cpu_ready.set();return self.cpu(time.time())
            agent=SimpleNamespace(config=config,root=Path(folder),metrics_sampler=SimpleNamespace(sample=cpu))
            telemetry=LauncherTelemetry(agent)
            with patch('launcher_agent.hardware_sample',side_effect=lambda **kw:gate.wait(2) or {'gpus':[]}), \
                 patch.object(InterfaceSampler,'sample',return_value=[]),patch.object(DiskSampler,'sample',return_value=[]):
                telemetry.start()
                try:
                    self.assertTrue(cpu_ready.wait(1))
                    self.assertEqual(telemetry.snapshot('worker')['percent'],25)
                finally:
                    telemetry.stopped.set();gate.set()
                    for thread in telemetry.threads:thread.join(2)
                self.assertTrue(all(not thread.is_alive() for thread in telemetry.threads))

    def test_authenticated_sender_owns_published_host_identity(self):
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        with tempfile.TemporaryDirectory() as folder:
            agent=Agent.__new__(Agent);agent.config=Path(folder)/'peers.json'
            key=bytes(range(32));cipher=AESGCM(key)
            agent.config.write_text(json.dumps({'name':'master','master':'master','peers':{'master':'a','worker':'b'},'key':key.hex()}))
            agent.lock=threading.RLock();agent.nonces={};agent.telemetry=self.telemetry()
            now=time.time();value={'from':'worker','to':'master','time':now,'nonce':'a'*32,'op':'metrics.publish',
                'args':{'session':'b'*32,'samples':[{'sequence':1,'kind':'cpu','sampled_at':now,'value':{**self.cpu(now),'host':'spoofed'}}]}}
            nonce=b'0'*12;raw=nonce+cipher.encrypt(nonce,json.dumps(value).encode(),VERSION.encode())
            asyncio.run(agent.process(raw))
            self.assertEqual(agent.telemetry.fleet({'worker'})['hosts'][0]['host'],'worker')

    def test_local_collectors_do_not_invoke_ssh(self):
        # Exercise the shipped scripts and deltas, not just mocked readers.
        target={'local':True,'host':'worker'}
        interface=InterfaceSampler();disk=DiskSampler();gpu=GpuSampler()
        for sampler in (interface,disk):
            self.assertIsInstance(sampler.sample(target),list)
        self.assertIsInstance(gpu.sample(target,{'gpus':[]}),dict)


class DeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_failed_tcp_retries_identical_samples_over_available_peer_channel(self):
        from unittest.mock import AsyncMock
        with tempfile.TemporaryDirectory() as folder:
            config=Path(folder)/'peers.json';config.write_text(json.dumps({'name':'worker','master':'master'}))
            agent=Agent.__new__(Agent);agent.config=config;agent.lock=threading.RLock()
            agent.telemetry=LauncherTelemetry(agent)
            now=time.time()
            agent.telemetry.record('cpu',{'status':'live','sampled_at':now,'percent':25,'cpus':4},now)
            gateway=JobGateway(agent)
            gateway.lan_mesh=SimpleNamespace(channels={'master':SimpleNamespace(readyState='open',bufferedAmount=0)})
            stopped=threading.Event();calls=[]
            async def call(op,batch,transport,timeout):
                calls.append((op,batch,transport,timeout))
                if len(calls)==1:raise TimeoutError('stalled peer')
                stopped.set()
                return {'session':batch['session'],'sequences':[row['sequence'] for row in batch['samples']]}
            gateway.master_call=call
            with patch('launcher_peers.asyncio.sleep',new=AsyncMock()):await gateway.publish_telemetry(stopped)
            self.assertEqual([call[2] for call in calls],['tcp','webrtc'])
            self.assertEqual(calls[0][1],calls[1][1])
            self.assertTrue(all(call[3]==6 for call in calls))

    async def test_encrypted_fragmented_publish_round_trip_over_peer_gateway(self):
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        from launcher_agent import VERSION
        key=bytes(range(32));cipher=AESGCM(key)
        with tempfile.TemporaryDirectory() as folder:
            gateways={}
            for name in ('master','worker'):
                agent=Agent.__new__(Agent);agent.config=Path(folder)/(name+'.json')
                agent.config.write_text(json.dumps({'name':name,'master':'master','peers':{'master':'a','worker':'b'},'key':key.hex()}))
                agent.lock=threading.RLock();agent.nonces={};agent.telemetry=LauncherTelemetry(agent)
                gateways[name]=JobGateway(agent)
            class Channel:
                readyState='open';bufferedAmount=0
                def __init__(self,sender,receiver):self.sender=sender;self.receiver=receiver;self.frames=0
                def send(self,raw):
                    self.frames+=1;gateways[self.receiver].receive(self.sender,json.loads(raw))
            forward=Channel('worker','master');reverse=Channel('master','worker')
            gateways['worker'].lan_mesh=SimpleNamespace(channels={'master':forward})
            gateways['master'].lan_mesh=SimpleNamespace(channels={'worker':reverse})
            now=time.time();cpu={'status':'live','sampled_at':now,'percent':25,'cpus':512,
                                'logical_cpus':[{'id':i,'percent':25} for i in range(512)]}
            value={'from':'worker','to':'master','time':now,'nonce':'f'*32,'op':'metrics.publish',
                   'args':{'session':'e'*32,'samples':[{'sequence':1,'kind':'cpu','sampled_at':now,'value':cpu}]}}
            nonce=b'1'*12;raw=nonce+cipher.encrypt(nonce,json.dumps(value).encode(),VERSION.encode())
            reply=await asyncio.wait_for(gateways['worker'].remote('master',raw),2)
            result=json.loads(cipher.decrypt(reply[:12],reply[12:],VERSION.encode()))
            self.assertTrue(result['ok'])
            self.assertEqual(result['result']['sequences'],[1])
            self.assertGreater(forward.frames,1)
            self.assertEqual(gateways['master'].agent.telemetry.fleet({'worker'})['hosts'][0]['percent'],25)
            self.assertEqual(gateways['worker'].pending,{})


    async def test_global_preference_switches_live_without_dropping_buffered_samples(self):
        from unittest.mock import AsyncMock
        with tempfile.TemporaryDirectory() as folder:
            agent=Agent.__new__(Agent);agent.config=Path(folder)/'worker.json';agent.lock=threading.RLock()
            agent.config.write_text(json.dumps({'name':'worker','master':'master'}))
            agent.telemetry=LauncherTelemetry(agent);now=time.time()
            sample={'status':'live','sampled_at':now,'cpus':4,'percent':25}
            agent.telemetry.record('cpu',sample,now)
            gateway=JobGateway(agent);stopped=threading.Event();transports=[];sequences=[]
            async def call(op,batch,transport,timeout):
                transports.append(transport);sequences.extend(row['sequence'] for row in batch['samples'])
                if len(transports)==1:agent.telemetry.record('cpu',sample,now)
                else:stopped.set()
                return {'session':batch['session'],'sequences':[row['sequence'] for row in batch['samples']],
                        'policy':{'transport':'webrtc','updated_at':now}}
            gateway.master_call=call
            with patch('launcher_peers.asyncio.sleep',new=AsyncMock()):await gateway.publish_telemetry(stopped)
            self.assertEqual(transports,['tcp','webrtc'])
            self.assertEqual(sequences,[1,2])
            self.assertEqual(agent.telemetry_policy()['transport'],'webrtc')

    async def test_tcp_reuses_connections_and_reconnects_without_signaling(self):
        import aiohttp
        from aiohttp import web
        key=bytes(range(32));transports=[]
        with tempfile.TemporaryDirectory() as folder:
            settings={'master':'master','peers':{'master':'127.0.0.1','worker':'127.0.0.1'},'key':key.hex()}
            receiver=Agent.__new__(Agent);receiver.config=Path(folder)/'master.json'
            receiver.config.write_text(json.dumps({**settings,'name':'master'}))
            receiver.lock=threading.RLock();receiver.nonces={};receiver.telemetry=LauncherTelemetry(receiver)
            sender=SimpleNamespace(config=Path(folder)/'worker.json')
            sender.config.write_text(json.dumps({**settings,'name':'worker'}))
            gateway=JobGateway(sender)
            async def handle(request):
                transports.append(request.transport)
                response=web.Response(body=await receiver.process(await request.read()))
                if len(transports)==2:response.force_close()
                return response
            app=web.Application();app.router.add_post('/agent/v1',handle)
            runner=web.AppRunner(app);await runner.setup()
            site=web.TCPSite(runner,'127.0.0.1',0);await site.start()
            port=site._server.sockets[0].getsockname()[1]
            real_session=aiohttp.ClientSession
            def session_factory(**kwargs):
                session=real_session(**kwargs);post=session.post
                session.post=lambda url,**kw:post(url.replace(':8790/',':'+str(port)+'/'),**kw)
                return session
            now=time.time()
            batch={'session':'a'*32,'samples':[{'sequence':1,'kind':'cpu','sampled_at':now,
                   'value':{'status':'live','sampled_at':now,'percent':25,'cpus':4}}]}
            try:
                with patch('aiohttp.ClientSession',side_effect=session_factory):
                    for _ in range(3):
                        reply=await gateway.master_call('metrics.publish',batch,'tcp',timeout=6)
                        self.assertEqual(reply['sequences'],[1])
                self.assertIs(transports[0],transports[1])
                self.assertIsNot(transports[1],transports[2])
                self.assertEqual(len(receiver.telemetry.fleet({'worker'})['events']),1)
                self.assertIsNone(gateway.mesh)
                self.assertIsNone(gateway.lan_mesh)
            finally:
                if gateway.telemetry_session:await gateway.telemetry_session.close()
                await runner.cleanup()


if __name__=='__main__':unittest.main()
