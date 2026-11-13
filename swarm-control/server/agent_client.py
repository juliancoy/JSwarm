"""Client for the encrypted launcher job protocol; no SSH fallback."""
import base64
import errno
import socket
import hashlib
import http.client
import threading
from urllib.parse import urlsplit
import json
import os
from pathlib import Path
import secrets
import time

PROTOCOL='lifetech-agent.v1'
UNPRIVILEGED_OPS={'status','metrics','metrics.publish','metrics.fleet','worker.policy','image.inspect','registry.read','tests.request','tests.status','stream.capabilities'}
SIGNING_KEY=Path.home()/'.config/map-master/controller-signing.key'
CONFIG=Path(os.getenv('LIFETECH_PEER_CONFIG',str(Path.home()/'.config/lifetech-launcher/peers.json')))


def multipath_connection(address,timeout=30,source_address=None,**kwargs):
    host,port=address
    last=None
    for family,kind,protocol,_,destination in socket.getaddrinfo(host,port,0,socket.SOCK_STREAM):
        sock=None
        try:
            try:sock=socket.socket(family,kind,getattr(socket,'IPPROTO_MPTCP',262))
            except OSError as error:
                if error.errno not in (errno.EPROTONOSUPPORT,errno.ENOPROTOOPT,errno.EINVAL,errno.EAFNOSUPPORT,errno.EPERM):raise
                sock=socket.socket(family,kind,protocol)
            sock.setsockopt(socket.IPPROTO_TCP,socket.TCP_NODELAY,1)
            sock.settimeout(timeout)
            if source_address:sock.bind(source_address)
            sock.connect(destination);return sock
        except OSError as error:
            last=error
            if sock:sock.close()
    if last:raise last
    raise OSError('No agent address available')


class AgentClient:
    def __init__(self, host):
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        config=json.loads(CONFIG.read_text())
        self.node,self.sender=host['host'],config['name']
        self.gateway=host.get('agent_route') in ('auto','webrtc') or self.node in config.get('wan_only',[])
        self.transport='webrtc' if host.get('agent_route')=='webrtc' or self.node in config.get('wan_only',[]) else 'auto'
        self.endpoint='http://127.0.0.1:8791' if self.gateway else host['agent_endpoint']
        if self.node not in config['peers']:raise ValueError('Agent is not enrolled')
        self.cipher=AESGCM(bytes.fromhex(config['key']))
        endpoint=urlsplit(self.endpoint)
        if endpoint.scheme not in ('http','https') or endpoint.username or endpoint.password or endpoint.path not in ('','/'):
            raise ValueError('Invalid agent endpoint')
        connection=http.client.HTTPSConnection if endpoint.scheme=='https' else http.client.HTTPConnection
        self.connection=connection(endpoint.hostname,endpoint.port,timeout=30)
        self.connection._create_connection=multipath_connection
        self.connection_lock=threading.Lock()

    def call(self, op, args=None, timeout=30):
        # Fresh authenticated requests may retry only operations without side effects.
        attempts = 3 if op in {'status', 'worker.policy', 'image.inspect', 'registry.read','resource.capacity','resource.gpus','stream.status','stream.list','stream.capabilities','worker.status','worker.records'} else 1
        for attempt in range(attempts):
            try:
                return self._request(op, args, timeout)
            except (OSError, http.client.HTTPException):
                if attempt + 1 == attempts:
                    raise
                self.close()
                time.sleep(attempt + 1)

    def _request(self, op, args=None, timeout=30):
        request_id=secrets.token_hex(16)
        value={'from':self.sender,'to':self.node,'time':time.time(),'nonce':request_id,'op':op,'args':args or {}}
        if op not in UNPRIVILEGED_OPS:
            from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
            key=Ed25519PrivateKey.from_private_bytes(SIGNING_KEY.read_bytes())
            value['controller_signature']=base64.b64encode(key.sign(json.dumps(value,sort_keys=True,separators=(',',':')).encode())).decode()
        nonce=secrets.token_bytes(12)
        data=nonce+self.cipher.encrypt(nonce,json.dumps(value).encode(),PROTOCOL.encode())
        with self.connection_lock:
            try:
                self.connection.timeout=timeout
                if self.connection.sock:self.connection.sock.settimeout(timeout)
                headers={'Content-Type':'application/octet-stream','X-Lifetech-Peer':self.node}
                if self.gateway:headers.update({'X-Lifetech-Transport':self.transport,'X-Lifetech-RPC-Timeout':str(min(timeout,180))})
                self.connection.request('POST','/agent/v1',body=data,headers=headers)
                response=self.connection.getresponse()
                raw=response.read(8*1024**2+1)
                if response.status!=200:raise RuntimeError('Agent HTTP status '+str(response.status))
            except Exception:
                self.connection.close()
                raise
        if len(raw)>8*1024**2:raise RuntimeError('Agent response exceeds limit')
        reply=json.loads(self.cipher.decrypt(raw[:12],raw[12:],PROTOCOL.encode()))
        if reply.get('nonce')!=request_id or reply.get('node')!=self.node or abs(time.time()-reply['time'])>60:raise RuntimeError('Agent response identity or freshness mismatch')
        if not reply.get('ok'):raise RuntimeError(reply.get('error','Agent operation failed'))
        return reply['result']

    def close(self):
        with self.connection_lock:self.connection.close()

    def upload(self,path,stopped=lambda:False,max_bytes_per_second=None):
        if max_bytes_per_second is not None and max_bytes_per_second<=0:raise ValueError('Upload rate must be positive')
        path=Path(path)
        with path.open('rb') as stream:digest=hashlib.file_digest(stream,'sha256').hexdigest()
        token=self.call('upload.begin',{'size':path.stat().st_size,'sha256':digest})['upload']
        offset=0;started=time.monotonic()
        with path.open('rb') as stream:
            while data:=stream.read(1024*1024):
                if stopped():raise InterruptedError('Agent upload stopped')
                result=self.call('upload.chunk',{'upload':token,'offset':offset,'data':base64.b64encode(data).decode()},timeout=120)
                offset+=len(data)
                if result['offset']!=offset:raise RuntimeError('Agent upload offset mismatch')
                if max_bytes_per_second:
                    delay=offset/max_bytes_per_second-(time.monotonic()-started)
                    if delay>0:time.sleep(delay)
        self.call('upload.finish',{'upload':token},timeout=180)
        return token


def wait_for_events(client,ident,cursor,stopped=lambda:False):
    """Resume by sequence after transport loss; never replay a mutation."""
    for attempt in range(3):
        if stopped():raise InterruptedError('Event wait stopped')
        try:
            return client.call('job.poll',{'id':ident,'cursor':cursor,'wait_seconds':20},timeout=45)
        except (OSError,http.client.HTTPException):
            if attempt==2:raise
            time.sleep(attempt+1)


class AgentSessions:
    def __init__(self,root,hosts,stopped,progress):
        self.root,self.hosts,self.stopped,self.progress=Path(root),hosts,stopped,progress
        self.expected_revision=None
        self.updater=None

    def reject(self,host,reason):
        host.update(status='unavailable',error=str(reason)[:800])
        host['session']={'status':'unavailable','verified':False,'error':str(reason)[:800]}
        self.progress(host,'unavailable',host['session'])

    def accessible(self):
        import sys
        sys.path.insert(0,str(self.root/'scripts'))
        from launcher_discovery import resolve_endpoint
        config=json.loads(CONFIG.read_text())
        reachable=[]
        for host in self.hosts:
            enrolled=config['peers'][host['host']]
            if host['host'] in config.get('wan_only',[]):
                if not config.get('internet_nodes',{}).get(host['host']) or not config.get('enrolled_machines',{}).get(host['host']):
                    self.reject(host,'WAN node lacks approved enrollment identity');continue
                host.update(agent_endpoint='http://127.0.0.1:8791',agent_route='webrtc',transport='webrtc',
                            lan_address=enrolled,expected_machine=config['enrolled_machines'][host['host']],discovery='Approved WAN enrollment')
                reachable.append(host);continue
            try:
                endpoint,addresses=resolve_endpoint(host['host'])
            except OSError as error:
                self.reject(host,str(error));continue
            host.update(agent_endpoint=endpoint,resolved_addresses=addresses,
                        lan_address=addresses[0],discovery='mDNS service hostname')
            reachable.append(host)
        return reachable

    def verify(self,hosts):
        if self.stopped():raise InterruptedError('Agent readiness stopped')
        if not hosts:return []
        import concurrent.futures
        if self.updater is None:
            from agent_updates import LauncherUpdates
            self.updater=LauncherUpdates.desired(self.stopped,self.progress)
            self.expected_revision=self.updater.revision
        expected=self.expected_revision
        def probe(host):
            if self.stopped():raise InterruptedError('Agent readiness stopped')
            try:
                client=AgentClient(host)
                try:value=client.call('status',timeout=5)
                except (OSError,TimeoutError):
                    client.close()
                    host['agent_endpoint']='http://127.0.0.1:8791'
                    host['transport']='webrtc'
                    client=AgentClient(host);value=client.call('status',timeout=30)
                if host.get('expected_machine') and value.get('machine')!=host['expected_machine']:raise RuntimeError('Approved WAN machine identity differs')
                if value.get('protocol')!=PROTOCOL:raise RuntimeError('Agent protocol mismatch')
                if host.get('transport')!='webrtc':
                    from launcher_discovery import attest_addresses
                    attest_addresses(host,value)
                if value.get('worker_policy',{}).get('accepting_jobs') is False:
                    host['worker_policy']=value['worker_policy']
                    raise RuntimeError(value['worker_policy']['reason'])
                value=self.updater.ensure(host,client,value,allow_compatible=True)
                required={'controller-signature','github-jobs','p2p-browser','cancel','events','layer-pull','registry-read','peer-layer-cache','mptcp-transport','file-queue','snapshot-stage','event-wait','shared-resources'}
                if value.get('protocol')!=PROTOCOL:raise RuntimeError('Agent protocol mismatch after launcher update')
                if not value.get('ready'):raise RuntimeError('Agent is not ready or is draining for an update')
                missing=required-set(value.get('capabilities',[]))
                if missing:raise RuntimeError('Agent capabilities missing: '+', '.join(sorted(missing))+' after launcher update')
                if value.get('source_revision')!=expected and host.get('launcher_update',{}).get('status')!='deferred':raise RuntimeError('Launcher update revision verification failed')
                if value.get('worker_policy',{}).get('accepting_jobs') is False:
                    host['worker_policy']=value['worker_policy']
                    raise RuntimeError(value['worker_policy']['reason'])
                if host.get('transport')!='webrtc':
                    from launcher_discovery import attest_addresses
                    attest_addresses(host,value)
                host['session']={'status':'ready','verified':True,'verified_at':time.time(),'transport':PROTOCOL,'machine':value['machine'],'capabilities':value['capabilities']}
                host['agent_status']=value
                host['hardware']=value.get('hardware',{})
                self.progress(host,'ready',host['session'])
                return host
            except InterruptedError:raise
            except Exception as error:self.reject(host,str(error));return None
        if not hosts:return []
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(8,len(hosts))) as pool:ready=[h for h in pool.map(probe,hosts) if h]
        unique={};result=[]
        for host in ready:
            identity=host['session']['machine']
            if identity in unique:self.reject(host,'Duplicate physical machine identity: '+unique[identity])
            else:unique[identity]=host['host'];result.append(host)
        return result
