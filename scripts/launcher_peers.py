"""Direct launcher WebRTC status mesh; encrypted LAN signaling, no cloud relay."""
import asyncio
try:
    from asyncio import timeout as operation_timeout
except ImportError:  # Python 3.10 native workers
    from async_timeout import timeout as operation_timeout
import json
import logging
import os
from pathlib import Path
import secrets
import threading
import time

CONFIG = Path.home() / '.config/lifetech-launcher/peers.json'
PORT = 8790
PROTOCOL = 'lifetech-launcher.v1'


def owner_launcher_url(path=CONFIG):
    """OS-owner launch creates a one-use browser capability; never trust loopback alone."""
    import hashlib
    path=Path(path);url='http://127.0.0.1:8791/swarm-node'
    config=json.loads(path.read_text()) if path.is_file() else {}
    if config.get('master_api') is not True or config.get('name')!=config.get('master','compai.local'):return url
    token=secrets.token_urlsafe(32)
    bootstrap=path.parent/'owner-browser-bootstrap.json';temporary=bootstrap.with_suffix('.tmp')
    temporary.write_text(json.dumps({'hash':hashlib.sha256(token.encode()).hexdigest(),'expires':time.time()+600}))
    temporary.chmod(0o600);temporary.replace(bootstrap)
    return url+'#owner='+token


def telemetry_tcp_socket(address_info):
    """MPTCP with bounded failure detection; independent of ICE and signaling."""
    import socket
    import errno
    from launcher_registry import transport_socket
    sock=transport_socket(address_info)
    try:
        sock.setsockopt(socket.IPPROTO_TCP,socket.TCP_NODELAY,1)
        sock.setsockopt(socket.SOL_SOCKET,socket.SO_KEEPALIVE,1)
        for option,value in (('TCP_KEEPIDLE',15),('TCP_KEEPINTVL',5),('TCP_KEEPCNT',3),('TCP_USER_TIMEOUT',10000)):
            if hasattr(socket,option):
                try:sock.setsockopt(socket.IPPROTO_TCP,getattr(socket,option),value)
                except OSError as error:
                    if error.errno!=errno.ENOPROTOOPT:raise
        return sock
    except Exception:
        sock.close();raise


def pipeline_configuration(settings):
    import ipaddress,re
    from launcher_discovery import service_hostname
    peers={name:(value['address'] if isinstance(value,dict) else value) for name,value in settings['peers'].items()}
    own=settings['name'];master=settings.get('master') or 'compai.local'
    if own not in peers or master not in peers:raise ValueError('Local node and coordinator must be enrolled')
    aliases={'jswarm.localhost':own,'map.local':master}
    for name,address in peers.items():
        short=name.removesuffix('.local')
        if not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?',short):raise ValueError('Invalid enrolled machine name')
        ip=ipaddress.ip_address(address)
        if ip.version!=4 or not ip.is_private or ip.is_loopback or ip.is_unspecified:raise ValueError('Expected an enrolled LAN IPv4 address')
        aliases[short+'.jswarm.localhost']=name
        aliases[service_hostname(name)]=name
    # Friendly names are optional; the full enrolled name always remains valid.
    for name in peers:
        short=name.removesuffix('.local').removeprefix('julian-')
        alias=short+'.jswarm.localhost'
        if alias not in aliases:aliases[alias]=name
    return {'name':own,'peers':peers,'aliases':aliases,'coordinator':master}


def pipeline_target(config,authority):
    from urllib.parse import urlsplit
    host=urlsplit('http://'+authority)
    if host.username or host.password or host.path or host.query or host.fragment:raise ValueError('Invalid Host')
    if host.port not in (None,80,8790,8791):raise ValueError('Unexpected web port')
    if host.hostname in ('localhost','127.0.0.1','::1'):return config['name']
    name=config['aliases'].get(host.hostname)
    if name:return name
    for name,address in config['peers'].items():
        if host.hostname in (name,address):return name
    raise ValueError('Unknown node web hostname')



def pipeline_assets():
    """Public UI assets are covered by the existing immutable node release."""
    import base64,gzip
    bundled=globals().get('PIPELINE_ASSETS_B64')
    if bundled:
        return {name:base64.b64decode(body) for name,body in json.loads(gzip.decompress(base64.b64decode(bundled))).items()}
    view=Path(__file__).resolve().parents[1]/'swarm-control/server/view'
    assets={str(path.relative_to(view)):path.read_bytes() for path in view.rglob('*') if path.is_file()}
    genome=view.parents[3]/'GenomeHarbor/web'
    assets.update({'genome/'+str(path.relative_to(genome)):path.read_bytes() for path in genome.rglob('*') if path.is_file()})
    return assets


def pipeline_web_root(config):
    """An owner-selected checkout supplies public UI files without a release bundle."""
    settings=json.loads(Path(config).read_text())
    workspace=settings.get('web_workspace') or os.environ.get('SWARM_WEB_WORKSPACE') or os.environ.get('SWARM_WORKSPACE')
    if not workspace:return None
    root=Path(workspace)
    if not root.is_absolute() or not (root/'JSwarm/swarm-control/server/view').is_dir():
        raise ValueError('Web workspace must be an absolute mounted Organization checkout')
    return root.resolve()


def pipeline_source_asset(workspace,name):
    """Resolve only public asset trees, including the versioned genome aliases."""
    from pathlib import PurePosixPath
    path=PurePosixPath(name)
    if not name or path.is_absolute() or '\\' in name or any(part.startswith('.') for part in path.parts):return None
    roots=(workspace/'JSwarm/swarm-control/server/view',workspace/'GenomeHarbor/web')
    target=(roots[0]/path).resolve()
    if not any(target.is_relative_to(root.resolve()) for root in roots):return None
    try:return target.read_bytes() if target.is_file() else None
    except FileNotFoundError:return None  # An editor can atomically replace a file between requests.


PIPELINE_NO_CACHE_HEADERS = {'Cache-Control':'no-store, no-cache, max-age=0, must-revalidate',
                             'Pragma':'no-cache','Expires':'0'}


def pipeline_source_revision(workspace):
    """Fingerprint public source metadata, never private state or file contents."""
    import hashlib
    roots=(workspace/'JSwarm/swarm-control/server/view',workspace/'GenomeHarbor/web')
    allowed=tuple(root.resolve() for root in roots)
    digest=hashlib.sha256()
    for index,root in enumerate(roots):
        for path in sorted(root.rglob('*')):
            if any(part.startswith('.') for part in path.relative_to(root).parts):continue
            try:
                target=path.resolve()
                if not any(target.is_relative_to(base) for base in allowed) or not target.is_file():continue
                stat=target.stat()
            except (FileNotFoundError, OSError):continue
            record=(index,str(path.relative_to(root)),stat.st_mtime_ns,stat.st_ctime_ns,stat.st_size,stat.st_ino)
            digest.update(json.dumps(record,separators=(',',':')).encode())
    return digest.hexdigest()


class NodePipelineWeb:
    """Pipeline routes in the existing node HTTP applications; no extra service."""
    def __init__(self,agent):
        self.agent=agent;self.workspace=pipeline_web_root(agent.config)
        self.assets=None if self.workspace else pipeline_assets()

    def local_version(self, config):
        # Report the running process, never the checkout or desired release.
        return {'node':config['name'], 'source_revision':getattr(self.agent,'source_revision',None),
                'protocol':'lifetech-agent.v1'}

    async def versions(self, config):
        from aiohttp import ClientSession,ClientTimeout
        from launcher_discovery import resolve_endpoint
        from datetime import datetime,timezone
        semaphore=asyncio.Semaphore(8)
        async with ClientSession(timeout=ClientTimeout(total=4),trust_env=False) as session:
            async def check(name):
                async with semaphore:
                    try:
                        if name==config['name']:
                            value=self.local_version(config)
                        else:
                            async with operation_timeout(5):
                                endpoint=(await asyncio.to_thread(resolve_endpoint,name))[0]
                                async with session.get(endpoint+'/api/jswarm-web/version',
                                        headers={'Host':name+':8790'},allow_redirects=False) as response:
                                    if response.status==404:
                                        return {'node':name,'status':'version-unavailable','source_revision':None}
                                    response.raise_for_status()
                                    body=await response.content.read(8193)
                                    if len(body)>8192:raise ValueError('Version response too large')
                                    value=json.loads(body)
                        import re
                        revision=value.get('source_revision')
                        if value.get('node')!=name or not isinstance(revision,str) or not re.fullmatch('[a-f0-9]{64}',revision):
                            raise ValueError('Invalid node version')
                        return {'node':name,'status':'connected','source_revision':revision,
                                'protocol':str(value.get('protocol',''))[:80]}
                    except Exception as error:
                        return {'node':name,'status':'unavailable','source_revision':None,
                                'error':type(error).__name__}
            nodes=await asyncio.gather(*(check(name) for name in sorted(config['peers'])))
        return {'schema':'jswarm-node-versions.v1','reporting_node':config['name'],
                'checked_at':datetime.now(timezone.utc).isoformat(),'nodes':nodes}

    async def handle(self,request):
        from aiohttp import web
        try:
            response=await self._handle(request)
        except web.HTTPException as error:
            error.headers.update(PIPELINE_NO_CACHE_HEADERS)
            raise
        if not response.prepared:
            response.headers.update(PIPELINE_NO_CACHE_HEADERS)
            for name in ('ETag','Last-Modified'):response.headers.pop(name,None)
        return response

    async def _handle(self,request):
        import ipaddress,mimetypes
        from aiohttp import web,ClientSession,ClientTimeout
        settings=json.loads(self.agent.config.read_text());config=pipeline_configuration(settings)
        try:
            remote=ipaddress.ip_address(request.remote)
            legacy_map=request.host.split(':')[0]=='map.local' and config['name']==config['coordinator'] and remote in ipaddress.ip_network(config['peers'][config['coordinator']]+'/24',strict=False)
            if not (remote.is_loopback or request.remote in config['peers'].values() or legacy_map):
                raise web.HTTPForbidden(text='Enrolled LAN clients only')
            node=pipeline_target(config,request.host)
            origin=request.headers.get('Origin')
            mcp=request.path=='/mcp' or request.path.startswith('/mcp/')
            if origin and origin!='http://'+request.host or request.method not in ('GET','HEAD','OPTIONS') and not origin and not mcp:
                raise web.HTTPForbidden(text='Same-origin request required')
        except ValueError:raise web.HTTPMisdirectedRequest()
        if node==config['name']:
            if request.path=='/api/jswarm-web/version' and request.method in ('GET','HEAD'):
                return web.json_response(self.local_version(config))
            if request.path=='/versions.json' and request.method in ('GET','HEAD'):
                return web.json_response(await self.versions(config))
            if request.path=='/api/jswarm-web/source-revision' and request.method=='GET':
                if not self.workspace:raise web.HTTPNotFound()
                revision=await asyncio.to_thread(pipeline_source_revision,self.workspace)
                return web.json_response({'revision':revision},headers=PIPELINE_NO_CACHE_HEADERS)
            if request.path=='/api/jswarm-web' and request.method=='GET':
                nodes=[{'name':name,'url':'http://'+name.removesuffix('.local')+'.jswarm.localhost/pipeline.html',
                        'direct_url':'http://'+address+'/pipeline.html'} for name,address in config['peers'].items()]
                return web.json_response({'node':config['name'],'nodes':nodes},headers={'Cache-Control':'no-store','X-JSwarm-Node':config['name']})
            if not request.path.startswith('/api/') and not mcp:
                if request.method not in ('GET','HEAD'):raise web.HTTPMethodNotAllowed(request.method,['GET','HEAD'])
                if request.path=='/':raise web.HTTPFound('/pipeline.html')
                if request.path=='/tests':raise web.HTTPFound('/tests.html'+('?' + request.query_string if request.query_string else ''))
                name=request.path.lstrip('/')
                body=pipeline_source_asset(self.workspace,name) if self.workspace else self.assets.get(name)
                if body is None:raise web.HTTPNotFound()
                if name.endswith('.html') and b'</head>' in body and b'src="/swarm-navigation.js"' not in body:
                    body=body.replace(b'</head>',b'<script type="module" src="/swarm-navigation.js"></script></head>',1)
                if self.workspace and name.endswith('.html'):
                    marker=b'</head>'
                    if marker in body and b'src="/jswarm-live.js"' not in body:
                        revision=await asyncio.to_thread(pipeline_source_revision,self.workspace)
                        bootstrap=('<script type="module" data-source-revision="'+revision+'" src="/jswarm-live.js"></script>').encode()
                        body=body.replace(marker,bootstrap+marker,1)
                return web.Response(body=body,headers={'Content-Type':mimetypes.guess_type(request.path)[0] or 'application/octet-stream',
                    'Cache-Control':'no-store','X-JSwarm-Node':config['name']})
            authority='map.local'
            if config['name']==config['coordinator']:
                address='127.0.0.1'
                port=8792 if request.path.startswith('/api/tasks') else 8795 if request.path=='/api/resources' or request.path.startswith('/api/v1/swarm/') or request.path=='/api/worker-controls' else 8089
                if request.path=='/api/v1/swarm/genome-results':
                    # Private genome evidence belongs to the owner API, not the LAN resource feed.
                    from urllib.parse import urlsplit
                    owner=urlsplit(settings.get('owner_web_origin','https://compai.lifetech.fyi'))
                    if owner.scheme!='https' or not owner.netloc or owner.path or owner.query or owner.fragment:raise web.HTTPBadGateway(text='Invalid owner API origin')
                    port=8091;authority=owner.netloc
                if request.path.startswith('/api/v1/master/codex/'):raise web.HTTPForbidden()
            else:
                from launcher_discovery import resolve_endpoint
                from urllib.parse import urlsplit
                try:address=urlsplit((await asyncio.to_thread(resolve_endpoint,config['coordinator']))[0]).hostname
                except OSError as error:raise web.HTTPBadGateway(text='Coordinator mDNS unavailable') from error
                port=8790
                authority=config['coordinator']+':8790'
        else:
            from launcher_discovery import resolve_endpoint
            from urllib.parse import urlsplit
            try:address=urlsplit((await asyncio.to_thread(resolve_endpoint,node))[0]).hostname
            except OSError as error:raise web.HTTPBadGateway(text='Node mDNS unavailable') from error
            port=8790;authority=request.host
        if request.headers.get('Transfer-Encoding') or len(request.headers.getall('Content-Length',[]))>1:raise web.HTTPBadRequest()
        excluded={'connection','keep-alive','proxy-authenticate','proxy-authorization','te','trailer','transfer-encoding','upgrade','host','origin','forwarded','x-forwarded-for','x-forwarded-host','accept-encoding'}
        headers={key:value for key,value in request.headers.items() if key.lower() not in excluded}
        headers['Host']=authority
        if origin:headers['Origin']=('https://' if port==8091 else 'http://')+authority
        try:
            async with ClientSession(timeout=ClientTimeout(total=60,connect=5),trust_env=False,auto_decompress=False) as session:
                async with session.request(request.method,'http://'+address+':'+str(port)+str(request.rel_url),
                        data=await request.read(),headers=headers,allow_redirects=False) as upstream:
                    response=web.StreamResponse(status=upstream.status,headers={key:value for key,value in upstream.headers.items()
                        if key.lower() not in {'connection','keep-alive','transfer-encoding','upgrade','etag','last-modified','cache-control','expires','pragma'}} | PIPELINE_NO_CACHE_HEADERS)
                    await response.prepare(request)
                    async for chunk in upstream.content.iter_chunked(65536):await response.write(chunk)
                    await response.write_eof();return response
        except (OSError,asyncio.TimeoutError):raise web.HTTPBadGateway(text='Node pipeline unavailable')


class Mesh:
    def __init__(self, config, prefix, agent=None):
        self.agent = agent
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        self.name, self.peers = config['name'], config['peers']
        self.addresses=config.get('addresses',[self.peers[self.name]])
        import hashlib
        folder = Path(__file__).resolve().parent
        self.config_revision = hashlib.sha256(json.dumps(self.peers,sort_keys=True).encode()).hexdigest()
        self.source_revision = hashlib.sha256(b''.join((folder/name).read_bytes() for name in ('launcher_peers.py','launcher_discovery.py','launcher_registration.py','launcher_agent.py','launcher_registry.py'))).hexdigest()
        if self.name not in self.peers or len(self.peers) > 24:
            raise ValueError('Mesh requires 1–24 enrolled launchers including this machine')
        self.cipher = AESGCM(bytes.fromhex(config['key']))
        self.prefix = 'lifetech-' if prefix == 'bmoremedtech-' else prefix
        self.links, self.channels, self.observations, self.nonces = {}, {}, {}, {}
        self.created = {}
        self.runner = None

    def seal(self, message):
        nonce = secrets.token_bytes(12)
        raw = json.dumps(message, separators=(',', ':')).encode()
        return nonce + self.cipher.encrypt(nonce, raw, PROTOCOL.encode())

    def open(self, data):
        if not 28 <= len(data) <= 65536:
            raise ValueError('Invalid signaling size')
        message = json.loads(self.cipher.decrypt(data[:12], data[12:], PROTOCOL.encode()))
        if (message['to'] != self.name or message['from'] not in self.peers
                or message['from'] == self.name or abs(time.time() - message['time']) > 60):
            raise ValueError('Invalid signaling identity or clock')
        return message

    def envelope(self, peer, description, nonce):
        return {'from': self.name, 'to': peer, 'time': time.time(), 'nonce': nonce,
                'type': description.type, 'sdp': description.sdp}

    async def drop(self, peer):
        pc = self.links.pop(peer, None)
        self.channels.pop(peer, None)
        self.observations.pop(peer, None)
        self.created.pop(peer, None)
        if pc:
            await pc.close()

    def attach(self, peer, channel):
        if channel.label != PROTOCOL or peer in self.channels:
            channel.close()
            return
        self.channels[peer] = channel

        @channel.on('message')
        def message(raw):
            if not isinstance(raw, str) or len(raw) > 32768:
                channel.close()
                return
            try:
                value = json.loads(raw)
                if value.get('type')=='agent-rpc' and getattr(self,'gateway',None):
                    self.gateway.receive(peer,value);return
                if value['type'] != 'status' or value['name'] != peer:
                    raise ValueError('Unexpected peer message')
                services = value['services']
                if not isinstance(services, list) or len(services) > 100:
                    raise ValueError('Invalid service list')
                clean = []
                for service in services:
                    if not isinstance(service, dict):
                        raise ValueError('Invalid service')
                    clean.append({key: str(service.get(key, ''))[:200] for key in ('name', 'state')})
                self.observations[peer] = {'received': time.time(), 'services': clean,
                                           'ready': value.get('ready') is True}
            except (ValueError, KeyError, TypeError):
                channel.close()

    def connection(self, peer):
        from aiortc import RTCPeerConnection, RTCConfiguration
        pc = RTCPeerConnection(getattr(self,'rtc_config',RTCConfiguration(iceServers=[])))
        self.links[peer] = pc
        self.created[peer] = time.monotonic()

        @pc.on('datachannel')
        def channel(dc):
            self.attach(peer, dc)

        return pc

    async def offer(self, request):
        from aiohttp import web
        from aiortc import RTCSessionDescription
        try:
            value = self.open(await request.read())
            peer, nonce = value['from'], value['nonce']
            self.nonces = {n: expiry for n, expiry in self.nonces.items() if expiry > time.time()}
            if (value['type'] != 'offer' or peer >= self.name or not isinstance(nonce, str)
                    or len(nonce) != 32 or nonce in self.nonces):
                raise ValueError('Invalid offer')
            self.nonces[nonce] = time.time() + 120
            if len(self.nonces) > 1024:
                raise ValueError('Too many offers')
            # Keep a healthy connection: do not let retries replace it.
            if peer in self.links and self.links[peer].connectionState == 'connected':
                raise ValueError('Peer already connected')
            await self.drop(peer)
            pc = self.connection(peer)
            try:
                async with operation_timeout(20):
                    await pc.setRemoteDescription(RTCSessionDescription(value['sdp'], 'offer'))
                    await pc.setLocalDescription(await pc.createAnswer())
            except Exception:
                await self.drop(peer)
                raise
            return web.Response(body=self.seal(self.envelope(peer, pc.localDescription, nonce)),
                                content_type='application/octet-stream')
        except Exception:
            raise web.HTTPBadRequest(text='Peer negotiation rejected')

    async def dial(self, peer, session):
        from aiortc import RTCSessionDescription
        pc = self.connection(peer)
        self.attach(peer, pc.createDataChannel(PROTOCOL))
        nonce = secrets.token_hex(16)
        try:
            async with operation_timeout(20):
                await pc.setLocalDescription(await pc.createOffer())
                endpoint = self.discovery.endpoint(peer)
                if endpoint is None:
                    raise ValueError('No current DNS-SD service announcement')
                host, port = endpoint
                url = f'http://{host}:{port}/offer'
                async with session.post(url, data=self.seal(self.envelope(peer, pc.localDescription, nonce)),
                                        allow_redirects=False) as response:
                    if response.status != 200:
                        raise ValueError('Peer rejected negotiation')
                    raw = await response.content.read(65537)
                value = self.open(raw)
                if value['from'] != peer or value['nonce'] != nonce or value['type'] != 'answer':
                    raise ValueError('Unexpected answer')
                await pc.setRemoteDescription(RTCSessionDescription(value['sdp'], 'answer'))
        except Exception as error:
            await self.drop(peer)
            logging.info('Launcher peer %s unavailable: %s', peer, type(error).__name__)

    async def status(self):
        def collect():
            import docker
            client = docker.from_env(timeout=5)
            try:
                return [{'name': container.name[:200], 'state': container.status[:200]}
                        for container in client.containers.list(all=True,filters={'name':self.prefix})
                        if container.name.startswith(self.prefix)][:100]
            finally:
                client.close()
        services = await asyncio.wait_for(asyncio.to_thread(collect), 8)
        return {'type': 'status', 'name': self.name, 'ready': Path('/tmp/launcher-ready').exists(),
                'services': services[:100]}

    async def run(self, stopped):
        from aiohttp import web, ClientSession, ClientTimeout, TCPConnector
        app = web.Application(client_max_size=2*1024**2)
        app.router.add_post('/offer', self.offer)
        if self.agent:
            app.router.add_post('/agent/v1',self.agent.handle)
            app.router.add_route('*','/{path:.*}',NodePipelineWeb(self.agent).handle)
        self.runner = web.AppRunner(app, access_log=None)
        await self.runner.setup()
        from launcher_discovery import Discovery
        self.discovery = Discovery(self.name, self.peers, PORT, self.addresses)
        from launcher_registry import transport_socket
        import socket
        for address in self.addresses:
            listener=transport_socket((socket.AF_INET,socket.SOCK_STREAM,0))
            try:
                listener.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
                listener.bind((address,PORT));listener.listen(128);listener.setblocking(False)
                await web.SockSite(self.runner,listener).start()
            except BaseException:
                listener.close()
                await self.runner.cleanup()
                self.discovery.close()
                raise
        print(f'Launcher WebRTC listening on LAN port {PORT}; {len(self.peers)-1} enrolled peers', flush=True)
        initial = {'name':self.name,'source_revision':self.source_revision,'config_revision':self.config_revision,'peers':{peer:{'connection':'disconnected'} for peer in self.peers if peer!=self.name}}
        path = Path('/tmp/launcher-peers.json')
        temporary = path.with_suffix('.tmp');temporary.write_text(json.dumps(initial));temporary.replace(path)
        try:
            await asyncio.to_thread(self.discovery.start)
            if self.agent:
                readiness=await asyncio.to_thread(self.agent.dispatch,'status',{})
                if not readiness['ready']:raise RuntimeError('Agent execution capabilities are unavailable')
                marker=Path('/tmp/launcher-agent-ready.json')
                temporary=marker.with_suffix('.tmp')
                temporary.write_text(json.dumps({'folder':str(Path(__file__).resolve().parent),'time':time.time()}));temporary.replace(marker)
            async with ClientSession(timeout=ClientTimeout(total=20), trust_env=False,
                                     connector=TCPConnector(socket_factory=transport_socket)) as session:
                while not stopped.is_set():
                    for peer, pc in list(self.links.items()):
                        observation = self.observations.get(peer)
                        stale = observation and time.time() - observation['received'] > 45
                        if (pc.connectionState in ('failed', 'closed') or stale
                                or (not observation and time.monotonic()-self.created[peer] > 45)):
                            await self.drop(peer)
                    await asyncio.gather(*(self.dial(peer, session) for peer in self.peers
                                           if self.name < peer and peer not in self.links))
                    try:
                        value = json.dumps(await self.status(), separators=(',', ':'))
                        for channel in list(self.channels.values()):
                            if channel.readyState == 'open' and channel.bufferedAmount < 65536:
                                channel.send(value)
                    except Exception as error:
                        logging.warning('Launcher status unavailable: %s', type(error).__name__)
                    snapshot = {peer: {'connection': self.links[peer].connectionState if peer in self.links else 'disconnected',
                                       **self.observations.get(peer, {})} for peer in self.peers if peer != self.name}
                    path = Path('/tmp/launcher-peers.json')
                    temporary = path.with_suffix('.tmp')
                    temporary.write_text(json.dumps({'name': self.name, 'source_revision':self.source_revision,'config_revision':self.config_revision, 'peers': snapshot}))
                    temporary.replace(path)
                    for _ in range(10):
                        if stopped.is_set():
                            break
                        await asyncio.sleep(1)
        finally:
            await asyncio.to_thread(self.discovery.close)
            await self.runner.cleanup()
            await asyncio.gather(*(self.drop(peer) for peer in list(self.links)))


def start(prefix):
    import subprocess
    import sys
    supervisor = Path(__file__).with_name('launcher_peer_supervisor.py')
    process = subprocess.Popen([sys.executable,'-B',str(supervisor),'--prefix',prefix])

    def stop():
        if process.poll() is None:
            process.terminate()
            try:process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill();process.wait()
    return stop


class InternetMesh(Mesh):
    """Renew credentials and negotiate WebRTC through the public signaling room."""
    def __init__(self, config, prefix):
        self.registration = config['internet']
        peer = self.registration['peer']
        super().__init__({'name':peer,'peers':{peer:peer},'key':secrets.token_hex(32)},prefix)
        self.socket = None
        self.expires = 0
        self.signaling_state = "connecting"
        self.last_error = None
        self.turn_available = None

    async def dial(self, peer, session=None):
        if peer in self.links:
            return
        pc = self.connection(peer)
        self.attach(peer, pc.createDataChannel(PROTOCOL))
        try:
            async with operation_timeout(20):
                await pc.setLocalDescription(await pc.createOffer())
                await self.socket.send_json({'type':'offer','to':peer,'description':{
                    'type':'offer','sdp':pc.localDescription.sdp}})
        except Exception:
            await self.drop(peer)
            raise

    async def signal(self, value):
        from aiortc import RTCSessionDescription
        kind = value.get('type')
        if kind == 'welcome':
            if value['peer'] != self.name or value['room'] != self.registration['room']:
                raise ValueError('Signaling identity mismatch')
            for peer in value['peers']:
                self.peers[peer] = peer
                if self.name < peer:
                    await self.dial(peer)
        elif kind == 'joined':
            peer = value['peer'];self.peers[peer] = peer
            if self.name < peer:
                await self.dial(peer)
        elif kind == 'left':
            peer = value['peer'];await self.drop(peer);self.peers.pop(peer,None)
        elif kind == 'offer':
            peer = value['from']
            if peer >= self.name or peer not in self.peers:
                raise ValueError('Unexpected offer')
            await self.drop(peer)
            pc = self.connection(peer)
            async with operation_timeout(20):
                await pc.setRemoteDescription(RTCSessionDescription(value['description']['sdp'],'offer'))
                await pc.setLocalDescription(await pc.createAnswer())
                await self.socket.send_json({'type':'answer','to':peer,'description':{
                    'type':'answer','sdp':pc.localDescription.sdp}})
        elif kind == 'answer':
            peer = value['from']
            pc = self.links.get(peer)
            if pc and pc.signalingState == 'have-local-offer':
                await pc.setRemoteDescription(RTCSessionDescription(value['description']['sdp'],'answer'))

    async def heartbeat(self, stopped):
        while not stopped.is_set():
            if time.time() >= self.expires-30:
                await self.socket.close()
                return
            for peer, pc in list(self.links.items()):
                seen = self.observations.get(peer)
                if (pc.connectionState in ('failed','closed') or (seen and time.time()-seen['received']>45)
                        or (not seen and time.monotonic()-self.created[peer]>45)):
                    await self.drop(peer)
            for peer in list(self.peers):
                if self.name < peer and peer not in self.links:
                    await self.dial(peer)
            try:
                value = json.dumps(await self.status(),separators=(',',':'))
                for channel in list(self.channels.values()):
                    if channel.readyState=='open' and channel.bufferedAmount<65536:
                        channel.send(value)
            except Exception as error:
                logging.warning('Launcher status unavailable: %s',type(error).__name__)
            snapshot = {'name':self.name,'transport':'webrtc','signaling':'internet',
                        'peers':{peer:{'connection':self.links[peer].connectionState if peer in self.links else 'disconnected',
                                       **self.observations.get(peer,{})} for peer in self.peers if peer!=self.name}}
            path = Path('/tmp/launcher-peers-internet.json')
            temporary = path.with_suffix('.tmp');temporary.write_text(json.dumps(snapshot));temporary.replace(path)
            for _ in range(10):
                if stopped.is_set():
                    await self.socket.close();return
                await asyncio.sleep(1)

    async def run(self, stopped):
        from aiohttp import ClientSession, ClientTimeout, WSMsgType
        from aiortc import RTCConfiguration, RTCIceServer
        from launcher_registration import service_url
        base = service_url(self.registration['url'])
        while not stopped.is_set():
            ticker = None
            try:
                async with ClientSession(timeout=ClientTimeout(total=30),trust_env=False,headers={'User-Agent':'LifeTech-Launcher/1.0'}) as session:
                    async with session.post(base+'/launchers/session',headers={'Authorization':'Bearer '+self.registration['credential']},
                            json={'room':self.registration['room'],'peer':self.name},allow_redirects=False) as response:
                        if response.status in (401,403):
                            Path('/tmp/launcher-peers-internet.json').write_text(json.dumps({
                                'name':self.name,'registration':'new-invitation-required','peers':{}}))
                            self.signaling_state='invitation-required'
                            logging.error('Launcher registration was revoked or expired; enroll with a new invitation')
                            return
                        if response.status!=200:raise ValueError('Device session rejected')
                        lease = await response.json()
                    self.expires = lease['expires'];token = lease['token']
                    async with session.get(base+'/ice',headers={'Authorization':'Bearer '+token},allow_redirects=False) as response:
                        if response.status!=200:raise ValueError('ICE configuration unavailable')
                        ice = await response.json()
                    self.rtc_config = RTCConfiguration(iceServers=[RTCIceServer(**server) for server in ice['iceServers']])
                    self.turn_available=any(str(url).startswith(('turn:','turns:')) for server in ice['iceServers'] for url in (server['urls'] if isinstance(server['urls'],list) else [server['urls']]))
                    url = base.replace('https://','wss://',1)+'/signal'
                    async with session.ws_connect(url,protocols=['archive.v1','auth.'+token],heartbeat=20,max_msg_size=65536) as ws:
                        self.socket = ws
                        self.signaling_state='connected';self.last_error=None
                        print('Launcher internet WebRTC signaling connected',flush=True)
                        ticker = asyncio.create_task(self.heartbeat(stopped))
                        def heartbeat_done(task):
                            if not task.cancelled() and task.exception() is not None:
                                logging.warning('Launcher heartbeat stopped: %s',type(task.exception()).__name__)
                                asyncio.create_task(ws.close())
                        ticker.add_done_callback(heartbeat_done)
                        async for message in ws:
                            if stopped.is_set():break
                            if message.type==WSMsgType.TEXT:
                                await self.signal(json.loads(message.data))
                            elif message.type in (WSMsgType.ERROR,WSMsgType.CLOSED):break
            except Exception as error:
                self.last_error=type(error).__name__
                logging.warning('Launcher internet session unavailable: %s',type(error).__name__)
            finally:
                if self.signaling_state!='invitation-required':self.signaling_state='reconnecting'
                if ticker:
                    ticker.cancel()
                    await asyncio.gather(ticker,return_exceptions=True)
                await asyncio.gather(*(self.drop(peer) for peer in list(self.links)))
                self.peers = {self.name:self.name}
            for _ in range(10):
                if stopped.is_set():break
                await asyncio.sleep(1)


def main():
    import argparse
    import signal
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--prefix',default='lifetech-')
    args=parser.parse_args()
    path=Path(os.getenv('LIFETECH_PEER_CONFIG',str(CONFIG)))
    stopped=threading.Event()
    signal.signal(signal.SIGTERM,lambda *_:stopped.set())
    signal.signal(signal.SIGINT,lambda *_:stopped.set())
    asyncio.run(supervise(path,args.prefix,stopped))


LAUNCHER_PAGE = '''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Swarm Node</title>
<style>body{font:16px system-ui;background:#eff5f3;color:#173833;margin:0}main{max-width:900px;margin:auto;padding:32px 20px}h1{font-size:36px;margin:10px 0}h2{font-size:20px}section{background:white;padding:24px;border:1px solid #c6d9d2;border-radius:16px;margin:18px 0}label{display:block;margin:14px 0}button,select,input,textarea{font:inherit;padding:12px;border:1px solid #a4bdb5;border-radius:8px;max-width:100%;box-sizing:border-box}button{background:#17665b;color:white;cursor:pointer;margin:4px 0}input,textarea{width:100%}textarea{min-height:110px}#phone-qr{display:block;max-width:100%;height:auto;margin-top:16px;background:white}#phone-code p{overflow-wrap:anywhere}#wan-service{overflow-wrap:anywhere}.connection-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px}.connection-grid div{background:#eff5f3;padding:14px;border-radius:10px}.connection-grid strong{display:block;margin-top:6px}.muted{color:#526c65}pre{white-space:pre-wrap;overflow-wrap:anywhere;font-size:13px}#message{min-height:24px}small{display:block;margin-top:8px}</style>
<main><span class="muted">SWARM NODE</span><h1>Swarm Node</h1><p id="identity">Connecting to your launcher…</p>
<section><h2>Connect from anywhere</h2><p class="muted">Connect this computer to Wi-Fi or Ethernet, then keep the launcher running. Your internet connection joins the swarm automatically.</p><div class="connection-grid"><div>Enrollment<strong id="enrollment">Checking…</strong></div><div>Internet signaling<strong id="signaling">Checking…</strong></div><div>Master WebRTC channel<strong id="master-channel">Checking…</strong></div></div><p id="connections"></p><p id="wan-service" class="muted"></p><p id="relay" class="muted"></p><details><summary>Network interfaces</summary><p id="interfaces" class="muted"></p></details><p><button id="remote">Test remote WebRTC</button> <button id="reconnect">Reconnect internet</button> <button id="check">Check LAN or internet</button></p><p id="connection-result" role="status"></p><details id="enrollment-form"><summary>Join or renew with a device invitation</summary><p>Use the invitation issued for this device by your swarm administrator. Credentials stay on this computer.</p><form id="enroll"><label>Import invitation JSON<input id="invitation-file" type="file" accept="application/json,.json"></label><label>Or paste invitation JSON<textarea id="invitation" autocomplete="off" spellcheck="false" placeholder='{"url":"https://your-signaling-service","token":"…"}'></textarea></label><button>Save invitation &amp; connect</button></form><p id="enrollment-result" role="status"></p></details></section>
<section id="phone-pairing"><h2>Pair a phone with CompAI</h2><p>Scan a one-time code to control CompAI’s Codex threads from your phone. Your browser stays paired for 30 days.</p><button id="phone-generate" disabled>Generate phone setup code</button><p id="phone-message" role="status">Checking owner launcher…</p><div id="phone-code" hidden><img id="phone-qr" alt="Scan to pair your phone with CompAI" width="280" height="280"><p><a id="phone-link" rel="noreferrer">Open setup link on your phone</a> <button id="phone-copy">Copy setup link</button></p><p id="phone-expiry" role="status"></p><small>Use this only for your own phone. Generating a new code replaces the previous unused code. Bookmark https://compai.lifetech.fyi/ after pairing.</small></div></section>
<section><h2>Contribute computing power</h2><p><button id="join-worker">Join swarm</button> <button id="pause-worker">Pause new jobs</button></p><label>Worker mode <select id="mode"><option value="auto">Automatic · AC power only</option><option value="always">Always accept jobs</option><option value="paused">Submit only · save battery</option></select></label><p id="power"></p><small class="muted">Already accepted jobs finish. You can submit work in every mode.</small></section>
<section><h2>Jobs on this computer</h2><p>Stop individual jobs, or pause new work and stop all accepted jobs. Core services keep running.</p><div id="local-jobs">Checking…</div><button id="stop-all-jobs">Pause &amp; stop all</button><p id="jobs-message" role="status"></p></section><section><h2>Request tests</h2><form id="request"><label>Suite names, separated by commas<input id="suites" placeholder="monitor"></label><label>Or repository paths, separated by commas<input id="paths" placeholder="OrgPortal/web/src"></label><small class="muted">Requests publish the master's current code and run tests on available workers. They do not deploy.</small><p><button>Submit authenticated request</button></p></form><p id="message" role="status"></p><details><summary>Master results</summary><pre id="result"></pre></details></section></main>
<script>const $=id=>document.getElementById(id);let phoneOwner=false,phoneGenerating=false,ownerSession='';async function api(path,body){const r=await fetch('/launcher/'+path,{headers:{...(ownerSession?{'X-Launcher-Owner':ownerSession}:{}),...(body===undefined?{}:{'Content-Type':'application/json'})},...(body===undefined?{}:{method:'POST',body:JSON.stringify(body)})});const d=await r.json();if(!r.ok)throw Error(d.error||r.status);return d}async function refresh(){try{const d=await api('status');$('identity').textContent=d.node+' · master '+d.master;$('mode').value=d.worker.mode;$('join-worker').disabled=!!d.worker.accepting_jobs;$('pause-worker').disabled=d.worker.mode==='paused';renderJobs(d.jobs||[]);$('power').textContent=d.worker.reason+(d.worker.batteries?.length?' · '+d.worker.batteries.map(b=>b.percent+'%').join(', '):'');$('interfaces').textContent=(d.interfaces||[]).map(i=>i.name+' · '+i.state+' · '+i.addresses.join(', ')).join(' | ');phoneOwner=!!d.phone_pairing;$('phone-generate').disabled=!phoneOwner||phoneGenerating;if(!$('phone-generate').dataset.used)$('phone-message').textContent=d.phone_pairing?'Ready to generate a private, one-time setup code.':d.phone_pairing_available?'Open ./run.py --swarm on CompAI to unlock phone setup codes.':'Generate setup codes on CompAI’s local owner launcher.';const w=d.wan||{};$('enrollment').textContent=w.enrollment||'Checking';$('signaling').textContent=w.signaling+(w.error?' · '+w.error:'');$('master-channel').textContent=w.master_channel||'disconnected';$('wan-service').textContent=w.service?'Signaling service: '+w.service:'Import your device invitation to connect over the internet.';$('relay').textContent=w.turn_available===false?'Direct WebRTC only. This service has no TURN relay; restrictive networks may prevent a connection.':w.turn_available?'TURN relay available for networks that block direct connections.':'Relay availability will appear after signaling connects.';const open=Object.values(d.webrtc_channels).filter(x=>x==='open').length;$('connections').textContent='LAN configured · '+(d.internet_enrolled?'Internet enrolled · '+open+' open WebRTC connection(s)':'Internet not enrolled');}catch(e){$('identity').textContent=e.message}}function renderJobs(jobs){const list=$('local-jobs');list.replaceChildren();if(!jobs.length){list.textContent='No active jobs.';return}for(const job of jobs){const row=document.createElement('p');const label=document.createElement('span');label.textContent=job.label+' · '+job.id+' · '+job.status+' ';row.append(label);const button=document.createElement('button');button.textContent=job.status==='stopping'?'Stopping…':'Stop';button.disabled=job.status==='stopping';button.onclick=()=>stopJobs({id:job.id,kind:job.kind},button);row.append(button);list.append(row)}}async function stopJobs(body,button){button.disabled=true;$('jobs-message').textContent='Requesting stop…';try{const result=await api('jobs-stop',body);const errors=(result.results||[]).filter(r=>r.error);$('jobs-message').textContent=errors.length?'Some jobs could not stop: '+errors.map(r=>r.id+': '+r.error).join('; '):body.all?'New jobs paused. Stop requested for all accepted jobs.':'Stop requested. Cleanup may take a moment.';await refresh()}catch(e){$('jobs-message').textContent=e.message}finally{button.disabled=false}}$('stop-all-jobs').onclick=()=>stopJobs({all:true},$('stop-all-jobs'));async function setWorkerMode(mode){try{await api('policy',{mode});await refresh()}catch(e){$('power').textContent=e.message}}$('mode').onchange=()=>setWorkerMode($('mode').value);$('join-worker').onclick=()=>setWorkerMode('auto');$('pause-worker').onclick=()=>setWorkerMode('paused');async function check(transport){$('connection-result').textContent='Checking…';try{const d=await api('master',{transport});$('connection-result').textContent='Authenticated master responded via '+(transport==='webrtc'?'WebRTC':'LAN-first routing');$('result').textContent=JSON.stringify(d,null,2)}catch(e){$('connection-result').textContent=e.message}}$('check').onclick=()=>check('auto');$('remote').onclick=()=>check('webrtc');$('reconnect').onclick=async()=>{try{const d=await api('reconnect',{});$('connection-result').textContent=d.status;await refresh()}catch(e){$('connection-result').textContent=e.message}};$('invitation-file').onchange=async e=>{const file=e.target.files[0];if(!file)return;if(file.size>16384){$('enrollment-result').textContent='Use an invitation under 16 KiB.';return}$('invitation').value=await file.text()};$('enroll').onsubmit=async e=>{e.preventDefault();const button=e.target.querySelector('button');button.disabled=true;try{const d=await api('enroll',JSON.parse($('invitation').value));$('invitation').value='';$('invitation-file').value='';$('enrollment-result').textContent=d.status;await refresh()}catch(e){$('enrollment-result').textContent=e.message}finally{button.disabled=false}};$('request').onsubmit=async e=>{e.preventDefault();const split=id=>$(id).value.split(',').map(s=>s.trim()).filter(Boolean);const body={suites:split('suites'),paths:split('paths')};if(!body.suites.length&&!body.paths.length){$('message').textContent='Choose suites or paths to test.';return}const button=e.target.querySelector('button');button.disabled=true;try{const d=await api('request',body);$('message').textContent='Request accepted: '+d.id;$('result').textContent=JSON.stringify(d,null,2)}catch(e){$('message').textContent=e.message}finally{button.disabled=false}};let phoneExpires=0;let phoneURL='';function phoneCountdown(){if(!phoneExpires)return;const seconds=Math.max(0,Math.ceil((phoneExpires-Date.now())/1000));$('phone-expiry').textContent=seconds?'Single use · expires in '+Math.floor(seconds/60)+':'+String(seconds%60).padStart(2,'0'):'Expired. Generate a new setup code.';if(!seconds){$('phone-code').hidden=true;phoneURL='';$('phone-qr').removeAttribute('src');$('phone-link').removeAttribute('href');}}$('phone-generate').onclick=async()=>{const button=$('phone-generate');phoneGenerating=true;button.disabled=true;button.dataset.used='true';$('phone-code').hidden=true;$('phone-message').textContent='Generating…';try{const d=await api('phone-pair',{});phoneURL=d.url;phoneExpires=d.expires_at*1000;$('phone-qr').src=d.qr;$('phone-link').href=phoneURL;$('phone-code').hidden=false;$('phone-message').textContent='Scan this code with your phone camera, or open the setup link on your phone.';phoneCountdown();}catch(e){$('phone-message').textContent=e.message}finally{phoneGenerating=false;button.disabled=!phoneOwner}};$('phone-copy').onclick=async()=>{try{await navigator.clipboard.writeText(phoneURL);$('phone-message').textContent='Setup link copied. Open it on your phone.'}catch{$('phone-message').textContent='Use the setup link or scan the QR code.'}};setInterval(phoneCountdown,1000);async function ownerStart(){const token=new URLSearchParams(location.hash.slice(1)).get('owner');if(token){history.replaceState(null,'',location.pathname);try{ownerSession=(await api('owner-unlock',{token})).session}catch(e){$('phone-message').textContent=e.message;}}await refresh();}void ownerStart();setInterval(refresh,10000);</script></html>'''


class JobGateway:
    """Loopback gateway and bounded encrypted agent RPC over WebRTC data channels."""
    def __init__(self, agent):
        self.agent=agent;self.telemetry_session=None;self.mesh=None;self.lan_mesh=None;self.pending={};self.parts={};self.inflight=set();self.runner=None;self.reconnect_generation=0;self.owner_sessions={}

    def owner_session_valid(self,headers):
        import hashlib
        token=headers.get('X-Launcher-Owner','')
        return isinstance(token,str) and 20<=len(token)<=128 and self.owner_sessions.get(hashlib.sha256(token.encode()).hexdigest(),0)>time.time()

    def unlock_owner(self,body):
        import hashlib
        if not isinstance(body,dict) or set(body)!={'token'} or not isinstance(body['token'],str) or not 20<=len(body['token'])<=128:raise ValueError('Invalid owner launch')
        config=json.loads(self.agent.config.read_text())
        if config.get('master_api') is not True or config.get('name')!=config.get('master','compai.local'):raise ValueError('Owner launch requires CompAI')
        path=self.agent.config.parent/'owner-browser-bootstrap.json'
        value=json.loads(path.read_text())
        if value['expires']<=time.time() or not secrets.compare_digest(value['hash'],hashlib.sha256(body['token'].encode()).hexdigest()):raise ValueError('Owner launch expired; open ./run.py --swarm on CompAI')
        path.unlink()
        token=secrets.token_urlsafe(32)
        self.owner_sessions={key:expiry for key,expiry in self.owner_sessions.items() if expiry>time.time()}
        self.owner_sessions[hashlib.sha256(token.encode()).hexdigest()]=time.time()+1800
        return token

    async def send(self, peer, ident, data, reply):
        import base64
        channel=next((mesh.channels[peer] for mesh in (self.lan_mesh,self.mesh) if mesh and peer in mesh.channels and mesh.channels[peer].readyState=='open'),None)
        if not channel or channel.readyState!='open':raise RuntimeError('Remote launcher is not connected')
        if len(data)>8*1024*1024:raise ValueError('RPC exceeds transport limit')
        async with operation_timeout(120):
            for index,offset in enumerate(range(0,len(data),12000)):
                while channel.bufferedAmount>262144:
                    if channel.readyState!='open':raise RuntimeError('Remote launcher disconnected')
                    await asyncio.sleep(.02)
                channel.send(json.dumps({'type':'agent-rpc','id':ident,'reply':reply,'seq':index,
                    'end':offset+12000>=len(data),'data':base64.b64encode(data[offset:offset+12000]).decode()}))

    def receive(self, peer, message):
        import base64,re
        config=json.loads(self.agent.config.read_text())
        if peer not in config.get('peers',{}) and peer not in config.get('internet_nodes',{}).values():raise ValueError('Unenrolled RPC peer')
        ident=message['id'];reply=message['reply']
        if not isinstance(ident,str) or not re.fullmatch('[a-f0-9]{32}',ident) or type(reply)!=bool:raise ValueError('Invalid RPC frame')
        key=(peer,ident,reply)
        if reply and (peer,ident) not in self.pending:raise ValueError('Unsolicited RPC response')
        self.parts={k:v for k,v in self.parts.items() if v['time']>time.monotonic()-120}
        if key not in self.parts:
            if message['seq']!=0 or len(self.parts)>=8:raise ValueError('RPC assembly limit')
            self.parts[key]={'data':bytearray(),'seq':0,'time':time.monotonic()}
        state=self.parts[key]
        if message['seq']!=state['seq'] or len(message['data'])>16000:raise ValueError('Invalid RPC sequence')
        state['data'].extend(base64.b64decode(message['data'],validate=True));state['seq']+=1
        if len(state['data'])>8*1024*1024:
            del self.parts[key];raise ValueError('RPC assembly limit')
        if message.get('end') is not True:return
        data=bytes(self.parts.pop(key)['data'])
        if reply:
            future=self.pending[(peer,ident)]
            if not future.done():future.set_result(data)
        else:
            if len(self.inflight)>=8:raise ValueError('RPC concurrency limit')
            async def respond():
                try:await self.send(peer,ident,await self.agent.process(data),True)
                except Exception as error:logging.warning('Remote agent request failed: %s',type(error).__name__)
            task=asyncio.create_task(respond());self.inflight.add(task);task.add_done_callback(self.inflight.discard)

    async def remote(self, node, raw):
        config=json.loads(self.agent.config.read_text());peer=node if self.lan_mesh and node in self.lan_mesh.channels and self.lan_mesh.channels[node].readyState=='open' else config.get('internet_nodes',{}).get(node)
        if not peer:raise RuntimeError('No internet enrollment for this node')
        if len(self.pending)>=8:raise RuntimeError('Remote request limit')
        ident=secrets.token_hex(16);future=asyncio.get_running_loop().create_future();self.pending[(peer,ident)]=future
        try:
            await self.send(peer,ident,raw,False)
            return await asyncio.wait_for(future,180)
        finally:self.pending.pop((peer,ident),None)

    async def request(self, request):
        from aiohttp import web,ClientSession,ClientTimeout,ClientConnectorError,ConnectionTimeoutError,TCPConnector
        from launcher_registry import transport_socket
        # This bridge never decrypts caller traffic, and only contacts enrolled peers.
        if request.headers.get('Origin'):raise web.HTTPForbidden()
        node=request.headers.get('X-Lifetech-Peer');config=json.loads(self.agent.config.read_text())
        if node not in config['peers']:raise web.HTTPForbidden()
        raw=await request.read()
        if node==config['name']:
            return await self.agent.handle(request)
        try:
            timeout=max(1,min(180,float(request.headers.get('X-Lifetech-RPC-Timeout','180'))))
            if request.headers.get('X-Lifetech-Transport')!='webrtc' and node not in config.get('wan_only',[]):
                try:
                    async with ClientSession(timeout=ClientTimeout(total=timeout,connect=min(3,timeout)),trust_env=False,connector=TCPConnector(socket_factory=transport_socket)) as session:
                        from launcher_discovery import resolve_endpoint
                        endpoint=(await asyncio.to_thread(resolve_endpoint,node))[0]
                        async with session.post(endpoint+'/agent/v1',data=raw,allow_redirects=False) as response:
                            if response.status!=200:raise RuntimeError('Agent rejected request')
                            data=bytearray()
                            async for chunk in response.content.iter_chunked(65536):
                                data.extend(chunk)
                                if len(data)>8*1024*1024:raise ValueError('RPC response too large')
                            data=bytes(data)
                            if len(data)>8*1024*1024:raise ValueError('RPC response too large')
                            return web.Response(body=data)
                except (ClientConnectorError,ConnectionTimeoutError):pass
            return web.Response(body=await asyncio.wait_for(self.remote(node,raw),timeout))
        except Exception as error:raise web.HTTPBadGateway(text=type(error).__name__+': '+str(error)[:200])

    def local_browser(self, request):
        from aiohttp import web
        origin=request.headers.get('Origin')
        if request.host not in ('127.0.0.1:8791','localhost:8791') or (origin and origin not in ('http://127.0.0.1:8791','http://localhost:8791')):
            raise web.HTTPForbidden()

    async def master_call(self, op, args, transport='auto', timeout=190):
        from aiohttp import ClientSession,ClientTimeout
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        config=json.loads(self.agent.config.read_text());node=config.get('master','compai.local')
        if node in config.get('wan_only',[]):transport='webrtc'
        cipher=AESGCM(bytes.fromhex(config['key']));ident=secrets.token_hex(16);nonce=secrets.token_bytes(12)
        value={'from':config['name'],'to':node,'time':time.time(),'nonce':ident,'op':op,'args':args}
        raw=nonce+cipher.encrypt(nonce,json.dumps(value).encode(),b'lifetech-agent.v1')
        async def read(session,url,headers):
            async with session.post(url,data=raw,headers=headers,allow_redirects=False,
                    timeout=ClientTimeout(total=timeout,connect=min(2,timeout),sock_read=min(4,timeout))) as response:
                if response.status!=200:raise RuntimeError((await response.text())[:300])
                result=bytearray()
                async for chunk in response.content.iter_chunked(65536):
                    result.extend(chunk)
                    if len(result)>8*1024*1024:raise ValueError('Master response exceeds limit')
                return bytes(result)
        if transport=='tcp':
            from aiohttp import TCPConnector
            if self.telemetry_session is None or self.telemetry_session.closed:
                self.telemetry_session=ClientSession(trust_env=False,
                    connector=TCPConnector(socket_factory=telemetry_tcp_socket,limit=2,limit_per_host=1,keepalive_timeout=30))
            from launcher_discovery import resolve_endpoint
            endpoint=(await asyncio.to_thread(resolve_endpoint,node))[0]
            raw=await read(self.telemetry_session,endpoint+'/agent/v1',
                           {'Content-Type':'application/octet-stream'})
        else:
            async with ClientSession(timeout=ClientTimeout(total=timeout),trust_env=False) as session:
                raw=await read(session,'http://127.0.0.1:8791/agent/v1',
                    {'X-Lifetech-Peer':node,'X-Lifetech-Transport':transport,'X-Lifetech-RPC-Timeout':str(min(timeout,180))})
        value=json.loads(cipher.decrypt(raw[:12],raw[12:],b'lifetech-agent.v1'))
        if value.get('nonce')!=ident or value.get('node')!=node or abs(time.time()-value['time'])>60:raise ValueError('Master response identity mismatch')
        if not value.get('ok'):raise RuntimeError(value.get('error','Master request failed'))
        return value['result']

    async def publish_telemetry(self, stopped):
        try:
            await self._publish_telemetry(stopped)
        finally:
            if self.telemetry_session is not None:
                await self.telemetry_session.close()
                self.telemetry_session=None

    async def _publish_telemetry(self, stopped):
        from aiohttp import ClientError
        acknowledged=set();delay=1
        preferred=self.agent.telemetry_policy()['transport']
        while not stopped.is_set():
            try:
                batch=self.agent.telemetry.pending(acknowledged)
                if not batch['samples']:
                    await asyncio.sleep(.25);continue
                config=json.loads(self.agent.config.read_text())
                if config['name']==config.get('master','compai.local'):
                    reply=self.agent.telemetry.accept(config['name'],batch)
                    reply['policy']=self.agent.telemetry_policy()
                else:
                    node=config.get('master','compai.local')
                    peer=config.get('internet_nodes',{}).get(node)
                    channel=next((mesh.channels[p] for mesh,p in ((self.lan_mesh,node),(self.mesh,peer)) if mesh and p in mesh.channels and mesh.channels[p].readyState=='open'),None)
                    try:
                        reply=await asyncio.wait_for(self.master_call('metrics.publish',batch,preferred,timeout=6),7)
                    except (OSError,RuntimeError,asyncio.TimeoutError,ClientError):
                        fallback='webrtc' if preferred=='tcp' else 'tcp'
                        if fallback=='webrtc' and (not channel or channel.readyState!='open' or channel.bufferedAmount>=65536):raise
                        reply=await asyncio.wait_for(self.master_call('metrics.publish',batch,fallback,timeout=6),7)
                if reply.get('session')!=batch['session'] or reply.get('sequences')!=[row['sequence'] for row in batch['samples']]:
                    raise ValueError('Telemetry acknowledgement mismatch')
                policy=reply.get('policy')
                if policy is not None and policy!=self.agent.telemetry_policy():self.agent.save_telemetry_policy(policy)
                preferred=self.agent.telemetry_policy()['transport']
                acknowledged.update(reply['sequences'])
                floor=self.agent.telemetry.sequence-256
                acknowledged={sequence for sequence in acknowledged if sequence>floor}
                delay=1
                await asyncio.sleep(.05)
            except asyncio.CancelledError:raise
            except Exception as error:
                logging.debug('Telemetry delivery retry: %s',type(error).__name__)
                await asyncio.sleep(delay+secrets.randbelow(250)/1000)
                delay=min(10,delay*2)

    async def browser(self, request):
        from aiohttp import web
        from launcher_agent import worker_policy
        self.local_browser(request)
        if request.path in ('/launcher','/swarm-node'):
            return web.Response(text=LAUNCHER_PAGE,content_type='text/html',headers={'Cache-Control':'no-store','X-Frame-Options':'DENY'})
        try:
            if request.method=='GET':
                config=json.loads(self.agent.config.read_text())
                import subprocess
                rows=json.loads(await asyncio.to_thread(subprocess.check_output,['ip','-j','addr'],text=True,timeout=5))
                interfaces=[{'name':r['ifname'],'state':r.get('operstate'),'addresses':[a['local'] for a in r.get('addr_info',[]) if a.get('scope')=='global']} for r in rows if r['ifname'].startswith(('en','eth','wl'))]
                peers={}
                if self.mesh:peers={p:c.readyState for p,c in self.mesh.channels.items()}
                enrollment={}
                try:enrollment=json.loads(Path('/tmp/launcher-enrollment.json').read_text())
                except (OSError,ValueError):pass
                remote=config.get('internet',{})
                wan={'service':remote.get('url'),'enrollment':enrollment.get('status','not-enrolled'),
                     'signaling':getattr(self.mesh,'signaling_state','not-connected'),
                     'error':getattr(self.mesh,'last_error',None),'turn_available':getattr(self.mesh,'turn_available',None),
                     'master_channel':peers.get(config.get('internet_nodes',{}).get(config.get('master','compai.local')),'disconnected')}
                return web.json_response({'node':config['name'],'worker':worker_policy(self.agent.config),
                    'jobs':self.agent.local_jobs(),
                    'routing':getattr(self.agent,'routing_status',{'status':'pending'}),
                    'lan':config.get('peers',{}),'internet_enrolled':bool(config.get('internet')),'webrtc_channels':peers,
                    'master':config.get('master','compai.local'),'interfaces':interfaces,'wan':wan,
                    'phone_pairing_available':config.get('master_api') is True and config['name']==config.get('master','compai.local') and (self.agent.config.parent/'owner-pairing-token').is_file(),
                    'phone_pairing':self.owner_session_valid(request.headers) and config.get('master_api') is True and config['name']==config.get('master','compai.local') and (self.agent.config.parent/'owner-pairing-token').is_file()},headers={'Cache-Control':'no-store'})
            body=await request.json()
            if request.path=='/launcher/jobs-stop':
                if body.get('all') is True and set(body)=={'all'}:
                    path=self.agent.config.parent/'worker-policy.json';temp=path.with_suffix('.tmp')
                    temp.write_text(json.dumps({'mode':'paused'}));temp.chmod(0o600);temp.replace(path)
                    jobs=self.agent.local_jobs();results=[]
                    for job in jobs:
                        try:results.append(await asyncio.to_thread(self.agent.stop_local_job,job['kind'],job['id']))
                        except Exception as error:results.append({'id':job['id'],'error':str(error)[:300]})
                    return web.json_response({'results':results,'paused':True})
                if set(body)!={'id','kind'}:raise ValueError('Choose a job or pause and stop all jobs')
                return web.json_response(await asyncio.to_thread(self.agent.stop_local_job,body['kind'],body['id']))
            if request.path=='/launcher/owner-unlock':
                return web.json_response({'session':self.unlock_owner(body)},headers={'Cache-Control':'no-store'})
            if request.path=='/launcher/phone-pair':
                if not self.owner_session_valid(request.headers):raise ValueError('Open ./run.py --swarm on CompAI to unlock phone pairing')
                if body!={}:raise ValueError('Pairing takes no arguments')
                config=json.loads(self.agent.config.read_text())
                if config.get('master_api') is not True or config['name']!=config.get('master','compai.local'):
                    raise ValueError('Create phone setup codes on the CompAI owner launcher')
                from aiohttp import ClientSession,ClientTimeout
                token=(self.agent.config.parent/'owner-pairing-token').read_text().strip()
                async with ClientSession(timeout=ClientTimeout(total=10),trust_env=False) as session:
                    async with session.post('http://127.0.0.1:8092/pairing',json={},headers={'Authorization':'Bearer '+token},allow_redirects=False) as response:
                        if response.status!=200:raise RuntimeError('Phone pairing service is unavailable; check the CompAI owner service')
                        return web.json_response(await response.json(),headers={'Cache-Control':'no-store'})
            if request.path=='/launcher/enroll':
                from launcher_registration import queue_invitation
                queue_invitation(body,self.agent.config)
                return web.json_response({'status':'Invitation saved; registration will retry automatically'})
            if request.path=='/launcher/reconnect':
                if body:raise ValueError('Reconnect takes no arguments')
                self.reconnect_generation+=1
                return web.json_response({'status':'Reconnecting internet signaling; LAN and running jobs remain active'})
            if request.path=='/launcher/policy':
                if body.get('mode') not in ('auto','always','paused'):raise ValueError('Invalid worker mode')
                path=self.agent.config.parent/'worker-policy.json';temp=path.with_suffix('.tmp')
                temp.write_text(json.dumps({'mode':body['mode']}));temp.chmod(0o600);temp.replace(path)
                if body['mode'] != 'paused':
                    from launcher_agent import start_local_peer_qc
                    await asyncio.to_thread(start_local_peer_qc, self.agent.config)
                return web.json_response(worker_policy(self.agent.config))
            if request.path=='/launcher/request':
                result=await self.master_call('tests.request',body)
            else:
                result=await self.master_call('tests.status',{},body.get('transport','auto'))
            return web.json_response(result)
        except Exception as error:return web.json_response({'error':str(error)[:500]},status=409)

    async def start(self):
        from aiohttp import web
        app=web.Application(client_max_size=2*1024*1024)
        app.router.add_post('/agent/v1',self.request)
        app.router.add_get('/launcher',self.browser)
        app.router.add_get('/swarm-node',self.browser)
        app.router.add_get('/launcher/status',self.browser)
        for route in ('policy','request','master','enroll','reconnect','phone-pair','owner-unlock','jobs-stop'):app.router.add_post('/launcher/'+route,self.browser)
        app.router.add_route('*','/{path:.*}',NodePipelineWeb(self.agent).handle)
        self.runner=web.AppRunner(app,access_log=None)
        await self.runner.setup()
        await web.TCPSite(self.runner,'127.0.0.1',8791).start()
        import socket
        if socket.has_ipv6:
            await web.TCPSite(self.runner,'::1',8791).start()


class EnrollmentPoller:
    """Retry only provisioned invitations; never auto-join without fleet authorization."""
    def __init__(self, path):
        self.path = Path(path)
        self.invitation = self.path.parent/'invitation.json'
        self.fingerprint = None
        self.next_attempt = 0
        self.delay = 5
        self.blocked = False
        self.last_state = None

    def state(self, value):
        if value != self.last_state:
            logging.info('Launcher enrollment: %s', value)
            self.last_state = value
        target = Path('/tmp/launcher-enrollment.json')
        temporary = target.with_suffix('.tmp')
        temporary.write_text(json.dumps({'status':value,'updated':time.time()}))
        temporary.replace(target)

    async def poll(self, config):
        import base64
        import hashlib
        import urllib.error
        from launcher_registration import register
        internet = config.get('internet',{})
        registration_state = 'registered'
        try:
            observed = json.loads(Path('/tmp/launcher-peers-internet.json').read_text())
            if observed.get('name')==internet.get('peer') and observed.get('registration')=='new-invitation-required':
                registration_state = 'new-invitation-required'
        except (OSError,ValueError):pass
        if not self.invitation.is_file():
            self.state(registration_state if internet and not internet.get('pending') else 'waiting-for-invitation')
            return config
        try:
            raw = self.invitation.read_bytes()
            if len(raw)>16384:raise ValueError('Invitation too large')
            fingerprint = hashlib.sha256(raw).hexdigest()
            if fingerprint != self.fingerprint:
                self.fingerprint = fingerprint
                self.next_attempt = 0
                self.delay = 5
                self.blocked = False
            value = json.loads(raw)
            if 'invitations' in value:
                if len(value['invitations'])!=1:raise ValueError('One device invitation required')
                value = value['invitations'][0]
            claims = json.loads(base64.urlsafe_b64decode(value['token'].split('.')[1]+'==='))
            if (internet.get('peer')==claims['peer'] and internet.get('room')==claims['room']
                    and not internet.get('pending') and not value.get('renew')):
                # A revoked credential requires a different invitation, not repeated enrollment.
                self.state(registration_state)
                return config
            if claims.get('aud')!='launcher-enroll-v1' or claims['exp']<=time.time():
                self.blocked = True
                self.state('new-invitation-required')
                return config
            if self.blocked or time.monotonic()<self.next_attempt:
                return config
            self.state('registering')
            await asyncio.to_thread(register,self.invitation,self.path)
            config = json.loads(self.path.read_text())
            if value.get('renew') and self.invitation.read_bytes()==raw:self.invitation.unlink()
            self.state('registered')
            self.delay = 5
            return config
        except urllib.error.HTTPError as error:
            if error.code in (400,401,403,409,410,422):
                self.blocked = True
                self.state('new-invitation-required')
            else:
                self.retry()
        except (OSError,TimeoutError):
            self.retry()
        except (ValueError,KeyError,TypeError,IndexError):
            self.blocked = True
            self.state('invalid-invitation')
        return config

    def retry(self):
        self.next_attempt = time.monotonic()+self.delay+secrets.randbelow(5)
        self.delay = min(self.delay*2,300)
        self.state('registration-retry-pending')


async def supervise(path,prefix,stopped):
    """Watch enrollment and run LAN/internet sessions independently without restart."""
    path = Path(path)
    poller = EnrollmentPoller(path)
    from launcher_agent import Agent
    agent=Agent(path,os.environ.get('LIFETECH_JOB_ROOT',str(path.parent/'jobs')))
    gateway=JobGateway(agent)
    await gateway.start()
    tasks = {}
    telemetry_task=asyncio.create_task(gateway.publish_telemetry(stopped),name='launcher-telemetry-push')
    routing_task=asyncio.create_task(maintain_routing(agent,stopped),name='launcher-routing')
    try:
        while not stopped.is_set():
            try:
                config = json.loads(path.read_text()) if path.is_file() else {}
                if not isinstance(config,dict):raise ValueError('Invalid peer configuration')
                config = await poller.poll(config)
                desired = {}
                if all(key in config for key in ('name','peers','key')):
                    desired['lan'] = {key:config[key] for key in ('name','peers','key')}
                    from launcher_registry import listener_addresses
                    import subprocess
                    rows=json.loads(await asyncio.to_thread(subprocess.check_output,['ip','-j','-4','addr'],text=True,timeout=5))
                    desired['lan']['addresses']=listener_addresses(rows)
                    if not desired['lan']['addresses']:
                        desired.pop('lan')
                if config.get('internet') and not config['internet'].get('pending'):
                    desired['internet'] = {'internet':config['internet'],'internet_nodes':config.get('internet_nodes',{}),'reconnect':gateway.reconnect_generation}
                for name,(version,task) in list(tasks.items()):
                    changed = name not in desired or version!=json.dumps(desired[name],sort_keys=True)
                    if changed:
                        task.cancel()
                        await asyncio.gather(task,return_exceptions=True)
                        del tasks[name]
                for name,value in desired.items():
                    if name in tasks:
                        continue
                    mesh = Mesh(value,prefix,agent) if name=='lan' else InternetMesh(value,prefix)
                    mesh.gateway=gateway
                    if name=='internet':gateway.mesh=mesh
                    else:gateway.lan_mesh=mesh
                    task = asyncio.create_task(mesh.run(stopped),name='launcher-'+name)
                    tasks[name] = (json.dumps(value,sort_keys=True),task)
                    def completed(done):
                        if not done.cancelled() and done.exception() is not None:
                            logging.error('Launcher peer session stopped: %s',type(done.exception()).__name__)
                    task.add_done_callback(completed)
                # A completed revoked internet session stays stopped until credentials change.
                # LAN startup failures retry, since a temporarily occupied port may become free.
                if 'lan' in tasks and tasks['lan'][1].done():
                    del tasks['lan']
            except (OSError,ValueError,KeyError,TypeError):
                poller.state('waiting-for-valid-configuration')
            for _ in range(5):
                if stopped.is_set():break
                await asyncio.sleep(1)
    finally:
        telemetry_task.cancel()
        routing_task.cancel()
        await asyncio.gather(telemetry_task,routing_task,return_exceptions=True)
        await gateway.runner.cleanup()
        for task in gateway.inflight:task.cancel()
        await asyncio.gather(*gateway.inflight,return_exceptions=True)
        await asyncio.to_thread(agent.close)
        agent.instance_lock.close()
        for _,task in tasks.values():task.cancel()
        await asyncio.gather(*(task for _,task in tasks.values()),return_exceptions=True)


async def maintain_routing(agent,stopped):
    """Reconcile hotplug/DHCP changes without restarting jobs or changing defaults."""
    from launcher_registry import configure_launcher_routing
    while not stopped.is_set():
        try:
            config=json.loads(agent.config.read_text())
            agent.routing_status=await asyncio.to_thread(configure_launcher_routing,config.get('peers',{}),config.get('name'))
        except Exception as error:
            agent.routing_status={'status':'unavailable','error':str(error)[-600:]}
            logging.warning('Launcher routing reconciliation unavailable: %s',type(error).__name__)
        agent.routing_status['checked_at']=time.time()
        for _ in range(30):
            if stopped.is_set():break
            await asyncio.sleep(1)


if __name__=='__main__':main()
