"""Loopback Docker registry bridge over authenticated agent requests."""
import base64
import errno
import hashlib
from pathlib import Path
import os
import socket
import shutil
import tempfile
from collections import deque
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import re
import secrets
import threading
import time
import urllib.request
import urllib.error
from urllib.parse import urlsplit

def sha256_stream(stream):
    """Hash bounded chunks on Python 3.10 workers as well as newer hosts."""
    digest=hashlib.sha256()
    for chunk in iter(lambda:stream.read(1024*1024),b''):digest.update(chunk)
    return digest.hexdigest()


PROTOCOL='lifetech-agent.v1'
CHUNK=4*1024*1024
CACHE_LOCK=threading.Lock()
PATH=re.compile(r'/v2/tests/[a-f0-9]{24}/(?:manifests|blobs)/sha256:[a-f0-9]{64}')


class MultipathSocket(socket.socket):
    @property
    def proto(self):
        # asyncio resolves even numeric addresses with sock.proto. libc's
        # getaddrinfo rejects protocol 262; DNS resolution still uses TCP.
        # SO_PROTOCOL on the actual descriptor remains IPPROTO_MPTCP.
        return socket.IPPROTO_TCP


def transport_socket(address_info):
    """Negotiate MPTCP when supported; kernel transparently accepts TCP peers."""
    family,kind,protocol,*_=address_info
    if family in (socket.AF_INET,socket.AF_INET6):
        try:return MultipathSocket(family,kind,getattr(socket,'IPPROTO_MPTCP',262))
        except OSError as error:
            if error.errno not in (errno.EPROTONOSUPPORT,errno.ENOPROTOOPT,errno.EINVAL,errno.EAFNOSUPPORT,errno.EPERM):raise
    return socket.socket(family,kind,protocol)


def lan_paths(rows, peers):
    """Only live physical IPv4 networks containing another enrolled LAN address."""
    import ipaddress
    addresses={ipaddress.ip_address(value) for value in peers.values()}
    paths=[]
    for row in rows:
        if not row['ifname'].startswith(('en','eth','wl')) or row.get('operstate')!='UP':continue
        if not 0<row['ifindex']<1024:continue
        for address in row.get('addr_info',[]):
            if address.get('family')!='inet' or address.get('scope')!='global':continue
            interface=ipaddress.ip_interface(address['local']+'/'+str(address['prefixlen']))
            if not any(peer!=interface.ip and peer in interface.network for peer in addresses):continue
            paths.append({'address':str(interface.ip),'network':str(interface.network),
                          'device':row['ifname'],'table':42000+row['ifindex'],
                          'priority':24000+row['ifindex']})
    return sorted(paths,key=lambda path:(path['device'].startswith('wl'),path['device'],path['address']))


def reconcile_lan_routing(peers):
    """Run in the host network namespace with NET_ADMIN, without host mounts."""
    import json
    import subprocess
    def ip(*args):
        result=subprocess.run(['ip',*map(str,args)],capture_output=True,text=True,timeout=5)
        if result.returncode:raise RuntimeError(result.stderr.strip()[:300])
        return result.stdout
    paths=lan_paths(json.loads(ip('-j','-4','addr')),peers)
    # Do not steal global/default routes. These source-and-destination rules
    # affect only traffic between an enrolled LAN and a local LAN address.
    rules=json.loads(ip('-N','-j','rule','show'))
    all_routes=json.loads(ip('-N','-j','route','show','table','all'))
    owned=lambda rule:str(rule.get('protocol'))=='242' and 24000<=rule.get('priority',0)<25024
    def rule_key(rule):
        import ipaddress
        destination=rule.get('dst','0.0.0.0')
        if '/' not in destination:destination+='/'+str(rule.get('dstlen',32))
        return (rule.get('priority'),rule.get('src','').removesuffix('/32'),
                str(ipaddress.ip_network(destination,strict=False)),str(rule.get('table')))
    desired={(p['priority'],p['address'],p['network'],str(p['table'])) for p in paths}
    desired_routes={(str(p['table']),p['network'],p['device'],p['address']) for p in paths}
    for rule in rules:
        key=rule_key(rule)
        if owned(rule) and key not in desired:
            ip('rule','del','priority',rule['priority'],'from',rule['src'],'to',rule_key(rule)[2],'table',rule['table'],'protocol',242)
    for route in all_routes:
        table=str(route.get('table',''))
        if (table.isdecimal() and 42000<int(table)<43024 and str(route.get('protocol'))=='242'
                and (table,route.get('dst'),route.get('dev'),route.get('prefsrc')) not in desired_routes):
            ip('route','del',route['dst'],'table',table,'protocol',242)
    for path in paths:
        if any(rule.get('priority')==path['priority'] and not owned(rule) for rule in rules):
            raise RuntimeError('LAN routing rule priority is already in use')
        routes=[route for route in all_routes if str(route.get('table'))==str(path['table'])]
        if any(str(route.get('protocol'))!='242' for route in routes):
            raise RuntimeError('LAN routing table is already in use')
        ip('route','replace',path['network'],'dev',path['device'],'src',path['address'],
           'table',path['table'],'protocol',242)
        key=(path['priority'],path['address'],path['network'],str(path['table']))
        if not any(owned(rule) and rule_key(rule)==key for rule in rules):
            ip('rule','add','priority',path['priority'],'from',path['address']+'/32',
               'to',path['network'],'table',path['table'],'protocol',242)
    # NetworkManager defaults to client-only endpoints. A launcher is also a
    # server: advertise its addresses so incoming connections can add paths.
    endpoints=json.loads(ip('-j','mptcp','endpoint','show'))
    for endpoint in list(endpoints):
        if 160<=endpoint['id']<224 and not any(endpoint['address']==p['address'] for p in paths):
            ip('mptcp','endpoint','delete','id',endpoint['id'])
            endpoints.remove(endpoint)
    for path in paths:
        existing=next((e for e in endpoints if e['address']==path['address']),None)
        if existing and existing.get('signal') and existing.get('subflow') and existing.get('dev')==path['device']:continue
        if existing:
            ident=existing['id']
            ip('mptcp','endpoint','delete','id',ident)
        else:
            ident=next((i for i in range(160,224) if i not in {e['id'] for e in endpoints}),None)
            if ident is None:raise RuntimeError('No launcher MPTCP endpoint IDs available')
        try:
            ip('mptcp','endpoint','add',path['address'],'dev',path['device'],'id',ident,'signal','subflow')
        except Exception:
            if existing:
                flags=[flag for flag in ('signal','subflow','backup','fullmesh') if existing.get(flag)]
                ip('mptcp','endpoint','add',existing['address'],'dev',existing['dev'],'id',ident,*flags)
            raise
        endpoints=[e for e in endpoints if e['id']!=ident]+[{'id':ident,'address':path['address']}]
    limits=ip('mptcp','limits','show').split()
    current=dict(zip(limits[::2],map(int,limits[1::2])))
    ip('mptcp','limits','set','subflows',max(4,current.get('subflows',0)),
       'add_addr_accepted',max(4,current.get('add_addr_accepted',0)))
    return {'status':'configured' if paths else 'no-lan-paths','paths':paths,'endpoints':json.loads(ip('-j','mptcp','endpoint','show')),
            'limits':ip('mptcp','limits','show').strip(),
            'scheduler':'kernel','connections':subprocess.check_output(['ss','-Mani'],text=True,timeout=5)[-12000:]}


def reconcile_node_web_ports(address):
    """Own only these NAT tables; HTTP stays in the unprivileged main node."""
    import hashlib,ipaddress,json,subprocess
    address=str(ipaddress.IPv4Address(address))
    if not ipaddress.ip_address(address).is_private or ipaddress.ip_address(address).is_loopback:
        raise ValueError('Expected an enrolled LAN address')
    definitions={
        'ip':f'chain prerouting {{ type nat hook prerouting priority -100; policy accept; ip daddr {address} tcp dport 80 dnat to {address}:8790; }}\n'
             f'chain output {{ type nat hook output priority -100; policy accept; ip daddr 127.0.0.1 tcp dport 80 redirect to :8791; ip daddr {address} tcp dport 80 dnat to {address}:8790; }}',
        'ip6':'chain output { type nat hook output priority -100; policy accept; ip6 daddr ::1 tcp dport 80 redirect to :8791; }'}
    marker='jswarm-node-ui:'+hashlib.sha256(json.dumps(definitions,sort_keys=True).encode()).hexdigest()
    def nft(*args,input=None):
        return subprocess.run(['/usr/sbin/nft',*args],input=input,text=True,capture_output=True,check=True,timeout=10).stdout
    tables=json.loads(nft('-j','list','tables'))['nftables']
    batch=[]
    for family,definition in definitions.items():
        exists=any(row.get('table',{}).get('name')=='jswarm_node_web' and row['table']['family']==family for row in tables)
        if exists:
            rows=json.loads(nft('-j','list','table',family,'jswarm_node_web'))['nftables']
            table=next(row['table'] for row in rows if 'table' in row)
            comment=table.get('comment','')
            if not comment.startswith('jswarm-node-ui:'):raise RuntimeError('Port forwarding table is owned by another application')
            if comment==marker:continue
            batch.append(f'delete table {family} jswarm_node_web')
        batch.append(f'table {family} jswarm_node_web {{ comment "{marker}";\n{definition}\n}}')
    if batch:nft('-f','-',input='\n'.join(batch)+'\n')
    return {'port':80,'address':address,'status':'configured'}


def configure_launcher_routing(peers,node_name=None):
    """Use a short-lived, narrowly privileged helper; the launcher stays unprivileged."""
    import inspect
    import docker
    source=inspect.getsource(lan_paths)+'\n'+inspect.getsource(reconcile_lan_routing)
    source+='\nimport json\n'
    if node_name:
        address=peers[node_name]
        if isinstance(address,dict):address=address['address']
        source+=inspect.getsource(reconcile_node_web_ports)+'\nreconcile_node_web_ports('+repr(address)+')\n'
    source+='print(json.dumps(reconcile_lan_routing('+repr(dict(peers))+')))\n'
    client=docker.from_env(timeout=20)
    container=None
    try:
        # The launcher image is already provisioned locally. No pull or host
        # socket mount is needed by this network-only helper. Host nft and its
        # libraries are mounted read-only when maintaining the public port.
        if any('rootless' in option for option in client.info().get('SecurityOptions',[])):
            return {'status':'unavailable','error':'Rootless Docker cannot configure host LAN routing; host network configuration is required'}
        container=client.containers.create('organization-launcher:webrtc-1',
            ['/usr/bin/python3' if node_name else 'python','-c',source],detach=True,network_mode='host',user='0:0',
            volumes={'/usr':{'bind':'/usr','mode':'ro'}} if node_name else {},
            cap_drop=['ALL'],cap_add=['NET_ADMIN'],read_only=True,
            security_opt=['no-new-privileges'],pids_limit=32,mem_limit='96m',
            labels={'organization.container-class':'launcher-routing'})
        container.start()
        result=container.wait(timeout=20)
        output=container.logs().decode()
        if result['StatusCode']:raise RuntimeError(output[-600:])
        return json.loads(output)
    finally:
        try:
            if container is not None:container.remove(force=True)
        finally:client.close()


def cache_read(cache,path,offset,head):
    if cache is None:return None
    digest=path.rsplit(':',1)[1]
    file=Path(cache)/digest
    try:
        with file.open('rb') as stream:
            total=os.fstat(stream.fileno()).st_size
            stream.seek(offset);data=b'' if head else stream.read(CHUNK)
        media=(Path(cache)/(digest+'.type')).read_text()
        if len(media)>256 or '\r' in media or '\n' in media:return None
        return {'data':base64.b64encode(data).decode(),'total':total,'type':media}
    except OSError:return None


def cache_publish(cache,temporary,path,media,total):
    with CACHE_LOCK:return _cache_publish(cache,temporary,path,media,total)


def _cache_publish(cache,temporary,path,media,total):
    """Only complete digest-verified objects become peer-readable."""
    if not isinstance(media,str) or len(media)>256 or '\r' in media or '\n' in media:raise ValueError('Invalid cached media type')
    temporary.flush();temporary.seek(0)
    if total>2*1024**3:return False
    if os.fstat(temporary.fileno()).st_size!=total:return False
    digest=path.rsplit(':',1)[1]
    if sha256_stream(temporary)!=digest:raise ValueError('Layer cache digest mismatch')
    cache=Path(cache);destination=cache/digest
    (cache/(digest+'.type')).write_text(media)
    os.replace(temporary.name,destination)
    # Bound retained compressed layers and manifests to 2 GiB, oldest first.
    files=sorted((p for p in cache.iterdir() if re.fullmatch('[a-f0-9]{64}',p.name)),key=lambda p:p.stat().st_mtime)
    size=sum(p.stat().st_size for p in files)
    for file in files:
        if size<=2*1024**3:break
        size-=file.stat().st_size;file.unlink(missing_ok=True);file.with_name(file.name+'.type').unlink(missing_ok=True)
    return True


def read(args,cache=None):
    path=args['path'];offset=args.get('offset',0)
    if not PATH.fullmatch(path):raise ValueError('Invalid registry path')
    if not isinstance(offset,int) or offset<0:raise ValueError('Invalid registry range')
    head=args.get('head') is True
    cached=cache_read(cache,path,offset,head)
    if cached is not None:return cached
    headers={'Accept':'application/vnd.oci.image.index.v1+json, application/vnd.oci.image.manifest.v1+json, application/vnd.docker.distribution.manifest.v2+json, application/vnd.docker.distribution.manifest.list.v2+json'}
    if not head:headers['Range']=f'bytes={offset}-{offset+CHUNK-1}'
    request=urllib.request.Request('http://127.0.0.1:18791'+path,headers=headers,method='HEAD' if head else 'GET')
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect())
    try:
        response=opener.open(request,timeout=30)
    except urllib.error.URLError as error:
        if not isinstance(error,urllib.error.HTTPError):return {'status':404,'data':'','total':0,'type':'application/json'}

        with error:
            detail=error.read(4096).decode(errors='replace')
            if error.code==404:
                return {'status':404,'data':'','total':0,'type':'application/json'}
            raise RuntimeError(f'Registry {head and "HEAD" or "GET"} {path}: HTTP {error.code}: {detail}') from error
    with response:
        if offset and response.status!=206:raise RuntimeError('Registry did not honor the blob range')
        total=int(response.headers.get('Content-Range','/').rsplit('/',1)[-1]) if response.status==206 else int(response.headers['Content-Length'])
        data=b'' if head else response.read(CHUNK)
        return {'data':base64.b64encode(data).decode(),'total':total,'type':response.headers.get('Content-Type','application/octet-stream')}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):raise RuntimeError('Registry redirects are forbidden')


def pull(agent,args):
    repository=args['repository'];digest=args['digest'];coordinator=args['coordinator']
    if not re.fullmatch('tests/[a-f0-9]{24}',repository) or not re.fullmatch('sha256:[a-f0-9]{64}',digest):raise ValueError('Invalid immutable registry reference')
    config=json.loads(agent.config.read_text())
    if coordinator not in config['peers']:raise ValueError('Registry coordinator is not enrolled')
    sources=list(dict.fromkeys(args.get('sources',[])+[coordinator]))
    if len(sources)>24 or any(node not in config['peers'] or node==config['name'] for node in sources):raise ValueError('Invalid enrolled image sources')
    cache=agent.config.parent/'layer-cache';cache.mkdir(mode=0o700,exist_ok=True)
    selected={};source_bytes={}
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    cipher=AESGCM(bytes.fromhex(config['key']))
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect())
    def request_chunk(path,offset,head=False):
        last=None
        for node in dict.fromkeys(([selected[path]] if path in selected else [])+sources):
            try:
                request_id=secrets.token_hex(16)
                payload={'from':config['name'],'to':node,'time':time.time(),'nonce':request_id,
                         'op':'registry.read','args':{'path':path,'offset':offset,'head':head}}
                nonce=secrets.token_bytes(12)
                request=urllib.request.Request('http://127.0.0.1:8791/agent/v1',
                    data=nonce+cipher.encrypt(nonce,json.dumps(payload).encode(),PROTOCOL.encode()),
                    headers={'Content-Type':'application/octet-stream','X-Lifetech-Peer':node})
                with opener.open(request,timeout=60) as response:raw=response.read(2*CHUNK)
                value=json.loads(cipher.decrypt(raw[:12],raw[12:],PROTOCOL.encode()))
                if value.get('nonce')!=request_id or value.get('node')!=node or abs(time.time()-value['time'])>60:raise ValueError('Invalid registry response identity or freshness')
                if not value.get('ok'):raise RuntimeError(value.get('error','Registry read failed'))
                result=value['result']
                if result.get('status')==404:continue
                selected[path]=node
                source_bytes[node]=source_bytes.get(node,0)+len(base64.b64decode(result.get('data',''),validate=True))
                return result
            except (OSError,ValueError,RuntimeError) as error:last=error
        if last:raise RuntimeError('No enrolled image source available') from last
        return {'status':404,'data':'','total':0,'type':'application/json'}
    errors=deque(maxlen=10)
    layers={}
    started=time.monotonic()
    samples=deque([(started,0)])
    with agent.lock:agent.image_progress[args['image']]={'transfer_phase':'starting','updated':time.time()}

    class Proxy(BaseHTTPRequestHandler):
        protocol_version='HTTP/1.1'
        def log_message(self,*_):pass
        def handle(self):
            # Docker probes HTTPS first, then permits HTTP for loopback registries.
            # Return an HTTP status line for a TLS probe so Go recognizes that fallback.
            if self.rfile.peek(1)[:1]==b'\x16':
                self.connection.sendall(b'HTTP/1.1 400 Bad Request\r\nContent-Length: 0\r\nConnection: close\r\n\r\n')
                self.close_connection=True
                return
            super().handle()
        def do_HEAD(self):self.serve(False)
        def do_GET(self):self.serve(True)
        def serve(self,body):
            # Docker may append a registry namespace query to immutable requests.
            path=urlsplit(self.path).path
            if path=='/v2/':
                self.send_response(200);self.send_header('Content-Length','2');self.send_header('Docker-Distribution-Api-Version','registry/2.0');self.end_headers()
                if body:self.wfile.write(b'{}')
                return
            if not PATH.fullmatch(path) or not path.startswith('/v2/'+repository+'/'):
                self.send_error(404);return
            sent=False;cached=None
            try:
                requested=self.headers.get('Range')
                match=re.fullmatch(r'bytes=(\d+)-(\d*)',requested) if requested else None
                if requested and not match:raise ValueError('Invalid registry byte range')
                offset=int(match[1]) if match else 0
                first=request_chunk(path,offset,head=not body)
                if first.get('status')==404:
                    errors.append('Registry resource missing: '+path)
                    self.send_error(404,'Registry resource missing');return
                total=first['total']
                end=min(total-1,int(match[2])) if match and match[2] else total-1
                if offset>end:raise ValueError('Invalid registry byte range bounds')
                self.send_response(206 if match else 200);self.send_header('Content-Type',first['type']);self.send_header('Content-Length',str(end-offset+1))
                self.send_header('Accept-Ranges','bytes')
                self.send_header('Docker-Content-Digest',path.rsplit('/',1)[-1])
                if match:self.send_header('Content-Range',f'bytes {offset}-{end}/{total}')
                self.send_header('Docker-Distribution-Api-Version','registry/2.0');self.end_headers();sent=True
                if not body:return
                if offset==0 and end==total-1 and total<=2*1024**3 and shutil.disk_usage(cache).free>total+128*1024**2:cached=tempfile.NamedTemporaryFile(mode='w+b',dir=cache,prefix='.partial-',delete=False)
                value=first
                while offset<=end:
                    data=base64.b64decode(value['data'],validate=True)[:end-offset+1]
                    if not data or len(data)>CHUNK or offset+len(data)>total or value['total']!=total:raise ValueError('Invalid registry chunk')
                    if cached:cached.write(data)
                    self.wfile.write(data);offset+=len(data)
                    if offset<=end:value=request_chunk(path,offset)
                if cached:cache_publish(cache,cached,path,first['type'],total)
            except Exception as error:
                errors.append(type(error).__name__+': '+str(error)[:400])
                self.close_connection=True
                if not sent:self.send_error(502,'Authenticated registry bridge failed')
            finally:
                if cached:cached.close();Path(cached.name).unlink(missing_ok=True)
    server=ThreadingHTTPServer(('127.0.0.1',0),Proxy);server.daemon_threads=True
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    reference=f'127.0.0.1:{server.server_port}/{repository}@{digest}'
    try:
        with agent.client() as client:
            for event in client.api.pull(reference,stream=True,decode=True):
                if event.get('error'):raise RuntimeError(event['error'])
                detail=event.get('progressDetail') or {}
                layer=event.get('id')
                if layer and event.get('status')=='Downloading' and detail.get('total'):
                    layers[layer]=(int(detail.get('current',0)),int(detail['total']))
                elif layer in layers and event.get('status') in ('Download complete','Pull complete'):
                    layers[layer]=(layers[layer][1],layers[layer][1])
                current=sum(row[0] for row in layers.values())
                total=sum(row[1] for row in layers.values())
                now=time.monotonic()
                samples.append((now,current))
                while len(samples)>2 and samples[1][0]<now-15:
                    samples.popleft()
                elapsed=now-samples[0][0]
                speed=max(0,current-samples[0][1])/elapsed if elapsed>=3 else 0
                remaining=max(0,total-current)
                with agent.lock:
                    agent.image_progress[args['image']]={'transfer_phase':'downloading' if remaining else 'finalizing',
                        'bytes_completed':current,'bytes_total':total,'bytes_per_second':speed,
                        'eta_seconds':remaining/speed if speed>0 and remaining else None,
                        'sources':dict(source_bytes),'updated':time.time()}
            image=client.images.get(reference)
            if agent.image_fingerprint(image)!=args['fingerprint']:raise ValueError('Registry image contents differ from coordinator')
            image.tag(args['image'])
            with agent.lock:agent.image_progress.pop(args['image'],None)
            return {'id':image.id,'source_bytes':dict(source_bytes)}
    except Exception as error:
        detail=('Registry bridge: '+errors[-1]+'; ') if errors else ''
        raise RuntimeError((detail+str(error))[:1000]) from error
    finally:
        with agent.lock:agent.image_progress.pop(args['image'],None)
        server.shutdown();server.server_close();thread.join(timeout=5)

