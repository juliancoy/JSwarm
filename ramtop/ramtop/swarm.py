"""Publish the local monitor through the enrolled swarm's authenticated protocol.

This supervised node component can be upgraded independently of busy Docker
workers. It never starts jobs, scans a subnet or changes host permissions.
"""
import argparse
from collections import deque
import http.client
import json
import os
from pathlib import Path
import secrets
import signal
import threading
import time
from .telemetry import PerformanceSampler

PROTOCOL='lifetech-agent.v1'


class Publisher:
    def __init__(self,config):
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        self.config=Path(config)
        self.session=secrets.token_hex(16)
        self.sequence=0
        self.pending=deque(maxlen=64)
        self.cipher_type=AESGCM

    def publish(self,value):
        config=json.loads(self.config.read_text())
        node=config['name'];master=config.get('master','compai.local')
        if node not in config['peers'] or master not in config['peers']:raise ValueError('Node is not enrolled')
        import ipaddress
        address=str(ipaddress.ip_address(config['peers'][master]))
        cipher=self.cipher_type(bytes.fromhex(config['key']))
        self.sequence+=1;request_id=secrets.token_hex(16)
        newest={'sequence':self.sequence,'kind':'performance','sampled_at':value['sampled_at'],'value':value}
        self.pending.append(newest)
        self.pending=deque((row for row in self.pending if row['sampled_at']>=time.time()-295),maxlen=64)
        selected=[]
        for row in [newest,*list(self.pending)[:-1]]:
            candidate=selected+[row]
            if len(candidate)<=16 and len(json.dumps(candidate,allow_nan=False))<245000:selected=candidate
        payload={'from':node,'to':master,'time':time.time(),'nonce':request_id,'op':'metrics.publish',
                 'args':{'session':self.session,'samples':selected}}
        nonce=secrets.token_bytes(12);body=nonce+cipher.encrypt(nonce,json.dumps(payload,allow_nan=False).encode(),PROTOCOL.encode())
        connection=http.client.HTTPConnection(address,8790,timeout=5)
        try:
            connection.request('POST','/agent/v1',body=body,headers={'Content-Type':'application/octet-stream','X-Lifetech-Peer':master})
            response=connection.getresponse();raw=response.read(262145)
            if response.status!=200 or len(raw)>262144:raise RuntimeError('Telemetry receiver unavailable')
            reply=json.loads(cipher.decrypt(raw[:12],raw[12:],PROTOCOL.encode()))
            if reply.get('nonce')!=request_id or reply.get('node')!=master or abs(time.time()-reply.get('time',0))>60:
                raise RuntimeError('Telemetry response identity or freshness mismatch')
            if not reply.get('ok'):raise RuntimeError('Telemetry receiver rejected monitor schema')
            ack=reply['result']
            if ack.get('session')!=self.session or self.sequence not in ack.get('sequences',[]):raise RuntimeError('Telemetry was not acknowledged')
            acknowledged=set(ack['sequences'])
            self.pending=deque((row for row in self.pending if row['sequence'] not in acknowledged),maxlen=64)
        finally:connection.close()


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',default=os.getenv('LIFETECH_PEER_CONFIG',str(Path.home()/'.config/lifetech-launcher/peers.json')))
    parser.add_argument('--once',action='store_true')
    args=parser.parse_args(argv)
    stopped=threading.Event()
    for signum in (signal.SIGTERM,signal.SIGINT):signal.signal(signum,lambda *_:stopped.set())
    sampler=PerformanceSampler();publisher=Publisher(args.config)
    state=Path(args.config).parent/'performance-state.json'
    try:
        while not stopped.is_set():
            started=time.monotonic()
            try:
                sample=sampler.sample();publisher.publish(sample)
                result={'status':'published','schema_version':1,'sampled_at':sample['sampled_at'],
                        'runtime':'builtin-swarm-monitor','revision':os.getenv('SWARM_MONITOR_REVISION'),'backend_tier':sample['memory']['backend_tier'],
                        'gpu_devices':sample.get('gpu',{}).get('devices',{})}
            except Exception as error:
                # Never log config, encryption keys, endpoint payloads or exception bodies.
                result={'status':'unavailable','error':type(error).__name__,'sampled_at':time.time()}
            temporary=state.with_suffix('.tmp');temporary.write_text(json.dumps(result));temporary.chmod(0o600);temporary.replace(state)
            if args.once:
                print(json.dumps(result));return 0 if result['status']=='published' else 1
            stopped.wait(max(.1,5-(time.monotonic()-started)))
    finally:sampler.close()


if __name__=='__main__':raise SystemExit(main())
