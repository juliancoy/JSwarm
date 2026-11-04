"""Owner-authorized, short-lived WebRTC delivery of one fixed Swarm Control test APK.

No shell, proxy, file path, browser-control or Codex operations on this channel.
Peer status/receipts live in memory for at most ten minutes; no device IDs stored.
"""
from paths import WORKSPACE
import asyncio
import base64
import concurrent.futures
import hashlib
import json
import os
from pathlib import Path
import secrets
import threading
import time

ARTIFACT = WORKSPACE/'.runtime/mobile/swarm-control-test.apk'
MAX_BYTES = 16*1024*1024
CHUNK = 16384
LIFETIME = 600


def artifact():
    if not ARTIFACT.is_file() or ARTIFACT.is_symlink():
        raise ValueError('Swarm Control test APK has not been published on CompAI')
    with ARTIFACT.open('rb') as stream:
        data=stream.read(MAX_BYTES+1)
    if not 0<len(data)<=MAX_BYTES or not data.startswith(b'PK'):
        raise ValueError('Invalid or oversized test APK')
    return data, {'name':'swarm-control-test.apk','package':'app.swarmcontrol.test','size':len(data),
                  'sha256':hashlib.sha256(data).hexdigest(),'chunkSize':CHUNK}


def request(data, manifest, message):
    if not isinstance(message,dict) or set(message)-{'id','op','offset','sha256'}:
        raise ValueError('Invalid deployment request')
    ident=message.get('id')
    if type(ident) is not int or not 0<=ident<2**31:
        raise ValueError('Invalid request ID')
    op=message.get('op')
    if op=='ping':return {'id':ident,'ok':True,'transport':'webrtc','app':'Swarm Control'}
    if op=='manifest':return {'id':ident,'ok':True,**manifest}
    if op=='chunk':
        offset=message.get('offset')
        if type(offset) is not int or offset<0 or offset>=len(data):raise ValueError('Invalid chunk offset')
        return {'id':ident,'ok':True,'offset':offset,'data':base64.b64encode(data[offset:offset+CHUNK]).decode()}
    if op=='received':
        if message.get('sha256')!=manifest['sha256']:raise ValueError('Receipt digest does not match the APK')
        return {'id':ident,'ok':True,'verified':True,'sha256':manifest['sha256']}
    raise ValueError('Unsupported deployment operation')


class Deployment:
    def __init__(self):
        self.loop=asyncio.new_event_loop()
        self.peers={}
        threading.Thread(target=self.loop.run_forever,daemon=True,name='swarm-control-p2p').start()
    async def close(self,ident):
        record=self.peers.pop(ident,None)
        if record:
            record['expiry'].cancel()
            await record['pc'].close()
    async def answer(self,body,valid):
        from aiortc import RTCPeerConnection,RTCSessionDescription,RTCConfiguration,RTCIceServer
        if not isinstance(body,dict) or set(body)!={'type','sdp'} or body.get('type')!='offer' or not isinstance(body.get('sdp'),str) or len(body['sdp'])>60000:
            raise ValueError('Expected a bounded WebRTC data-channel offer')
        media=[line for line in body['sdp'].splitlines() if line.startswith('m=')]
        if len(media)!=1 or not media[0].startswith('m=application '):raise ValueError('Only a data channel is supported')
        if not valid():raise PermissionError('Device session expired')
        if len(self.peers)>=4:raise ValueError('Four test deployment connections are already active; disconnect or wait for expiry')
        data,manifest=artifact()
        pc=RTCPeerConnection(RTCConfiguration(iceServers=[RTCIceServer(urls='stun:stun.l.google.com:19302')]))
        ident=secrets.token_hex(16)
        record={'pc':pc,'created':time.time(),'received':False,'valid':valid,'channels':0,'window':time.monotonic(),'requests':0}
        record['expiry']=self.loop.call_later(LIFETIME,lambda:asyncio.create_task(self.close(ident)))
        self.peers[ident]=record
        @pc.on('connectionstatechange')
        async def state():
            if pc.connectionState in ('failed','closed'):await self.close(ident)
        @pc.on('datachannel')
        def channel(dc):
            record['channels']+=1
            if record['channels']!=1 or dc.label!='swarm-control-test.v1':dc.close();return
            @dc.on('message')
            def incoming(raw):
                if time.time()-record['created']>=LIFETIME or not valid():
                    dc.close();asyncio.create_task(self.close(ident));return
                if not isinstance(raw,str) or len(raw)>4096 or dc.bufferedAmount>256*1024:
                    dc.close();return
                now=time.monotonic()
                if now-record['window']>=1:record['requests']=0;record['window']=now
                record['requests']+=1
                if record['requests']>100:dc.close();return
                try:
                    value=json.loads(raw);response=request(data,manifest,value)
                    if value['op']=='received':record['received']=True
                except (ValueError,TypeError):
                    response={'id':None,'ok':False,'error':'Invalid deployment request'}
                if dc.readyState=='open':dc.send(json.dumps(response,separators=(',',':')))
        try:
            await pc.setRemoteDescription(RTCSessionDescription(body['sdp'],'offer'))
            await pc.setLocalDescription(await pc.createAnswer())
            return {'type':pc.localDescription.type,'sdp':pc.localDescription.sdp,'expiresIn':LIFETIME}
        except Exception:
            await self.close(ident)
            raise
    def offer(self,body,valid):
        future=asyncio.run_coroutine_threadsafe(self.answer(body,valid),self.loop)
        try:return future.result(timeout=35)
        except concurrent.futures.TimeoutError:
            future.cancel();raise ValueError('WebRTC signaling timed out') from None
    async def snapshot(self):
        for ident,record in list(self.peers.items()):
            if not record['valid']():await self.close(ident)
        return [{'state':r['pc'].connectionState,'apkReceived':r['received'],'expiresIn':max(0,int(LIFETIME-time.time()+r['created']))} for r in self.peers.values()]
    def status(self):
        try:_,manifest=artifact()
        except ValueError:manifest=None
        peers=asyncio.run_coroutine_threadsafe(self.snapshot(),self.loop).result(timeout=5)
        return {'app':manifest,'peers':peers,'transport':'webrtc','turnConfigured':False,'installation':'Android requires approval on the phone'}

_manager=None
_lock=threading.Lock()
def manager():
    global _manager
    with _lock:
        if _manager is None:_manager=Deployment()
    return _manager
