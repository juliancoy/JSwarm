"""Serve the packaged pipeline on enrolled nodes and route local machine aliases.

The UI is an independent immutable image deployment. Node agents and accepted
compute jobs are never replaced. Enrollment remains the only machine inventory.
"""
import argparse
import hashlib
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
import mimetypes
from pathlib import Path
import re
import socket
import subprocess
import threading
import time
from urllib.parse import urlsplit

CONTAINER='swarm-node-web'
PORT=8797
LABEL='org.lifetech.service'
SERVICE='node-web'
VIEW=Path(__file__).resolve().parents[1]/'swarm-control/server/view'
HOP={'connection','keep-alive','proxy-authenticate','proxy-authorization','te','trailer','transfer-encoding','upgrade'}


def configuration(settings):
    peers={name:(value['address'] if isinstance(value,dict) else value) for name,value in settings['peers'].items()}
    own=settings['name'];master=settings.get('master') or 'compai.local'
    if own not in peers or master not in peers:raise ValueError('Local node and coordinator must be enrolled')
    aliases={'jswarm.localhost':own}
    for name,address in peers.items():
        short=name.removesuffix('.local')
        if not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?',short):raise ValueError('Invalid enrolled machine name')
        ip=ipaddress.ip_address(address)
        if ip.version!=4 or not ip.is_private or ip.is_loopback or ip.is_unspecified:raise ValueError('Expected an enrolled LAN IPv4 address')
        aliases[short+'.jswarm.localhost']=name
    # Friendly names are optional; the full enrolled name always remains valid.
    for name in peers:
        short=name.removesuffix('.local').removeprefix('julian-')
        alias=short+'.jswarm.localhost'
        if alias not in aliases:aliases[alias]=name
    return {'name':own,'peers':peers,'aliases':aliases,'coordinator':master}


def target(config,authority):
    host=urlsplit('http://'+authority)
    if host.username or host.password or host.path or host.query or host.fragment:raise ValueError('Invalid Host')
    if host.port not in (None,80,PORT):raise ValueError('Unexpected web port')
    name=config['aliases'].get(host.hostname)
    if name:return name
    for name,address in config['peers'].items():
        if host.hostname in (name,address):return name
    raise ValueError('Unknown node web hostname')


def handler(config,view=VIEW):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):self.handle_request()
        def do_HEAD(self):self.handle_request()
        def do_POST(self):self.handle_request()

        def handle_request(self):
            try:
                if not (ipaddress.ip_address(self.client_address[0]).is_loopback or self.client_address[0] in config['peers'].values()):
                    self.send_error(403,'Enrolled LAN clients only');return
                node=target(config,self.headers.get('Host',''))
                origin=self.headers.get('Origin')
                if origin and origin!='http://'+self.headers['Host']:
                    self.send_error(403,'Same-origin request required');return
                if self.command=='POST' and not origin:
                    self.send_error(403,'Same-origin request required');return
            except ValueError:
                self.send_error(421,'Unknown node web hostname');return
            path=urlsplit(self.path)
            if path.scheme or path.netloc:
                self.send_error(400,'Relative request path required');return
            if path.path=='/health' and self.command!='POST':
                self.json({'node':config['name'],'service':SERVICE});return
            if node!=config['name']:
                self.proxy(config['peers'][node],PORT,self.headers['Host']);return
            if path.path=='/api/jswarm-web' and self.command=='GET':
                nodes=[]
                for name,address in config['peers'].items():
                    nodes.append({'name':name,'url':'http://'+name.removesuffix('.local')+'.jswarm.localhost/pipeline.html',
                                  'direct_url':'http://'+address+':'+str(PORT)+'/pipeline.html'})
                self.json({'node':config['name'],'nodes':nodes});return
            if path.path.startswith('/api/'):
                if self.command=='POST' and path.path not in ('/api/tasks/machine/control','/api/worker-controls'):
                    self.send_error(404);return
                self.proxy(config['peers'][config['coordinator']],80,'map.local');return
            if self.command=='POST':self.send_error(405);return
            if path.path=='/':
                self.send_response(302);self.send_header('Location','/pipeline.html');self.send_header('Content-Length','0');self.end_headers();return
            # File names are URL decoded once and must remain inside packaged assets.
            from urllib.parse import unquote
            asset=(view/unquote(path.path).lstrip('/')).resolve()
            if not asset.is_relative_to(view.resolve()) or not asset.is_file():self.send_error(404);return
            self.send_response(200)
            self.send_header('Content-Type',mimetypes.guess_type(asset.name)[0] or 'application/octet-stream')
            self.send_header('Content-Length',str(asset.stat().st_size));self.send_header('Cache-Control','no-store')
            self.send_header('X-JSwarm-Node',config['name']);self.end_headers()
            if self.command!='HEAD':
                with asset.open('rb') as source:
                    import shutil
                    shutil.copyfileobj(source,self.wfile)

        def json(self,value):
            body=json.dumps(value).encode();self.send_response(200)
            self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(body)))
            self.send_header('Cache-Control','no-store');self.send_header('X-JSwarm-Node',config['name']);self.end_headers()
            if self.command!='HEAD':self.wfile.write(body)

        def proxy(self,address,port,authority):
            if self.headers.get('Transfer-Encoding') or len(self.headers.get_all('Content-Length',[]))>1:
                self.send_error(400,'Expected a bounded request');return
            try:size=int(self.headers.get('Content-Length','0'))
            except ValueError:self.send_error(400);return
            if not 0<=size<=4*1024*1024:self.send_error(413);return
            body=self.rfile.read(size) if size else None
            # Only a fixed, enrolled destination is selected. Never forward proxy headers.
            headers={key:value for key,value in self.headers.items() if key.lower() not in HOP|{'host','origin','forwarded','x-forwarded-for','x-forwarded-host','accept-encoding'}}
            headers['Host']=authority
            if self.headers.get('Origin'):headers['Origin']='http://'+authority
            connection=http.client.HTTPConnection(address,port,timeout=30)
            sent=False
            try:
                connection.request(self.command,self.path,body=body,headers=headers);response=connection.getresponse()
                self.send_response(response.status)
                for key,value in response.getheaders():
                    if key.lower() not in HOP:self.send_header(key,value)
                self.send_header('Connection','close');self.end_headers();sent=True
                if self.command!='HEAD':
                    while chunk:=response.read1(65536):self.wfile.write(chunk)
            except (OSError,http.client.HTTPException):
                if not sent:self.send_error(502,'Node web service unavailable')
            finally:connection.close();self.close_connection=True
    return Handler


def serve(config):
    servers=[]
    # Each machine serves its own immutable frontend on its enrolled LAN address.
    # Loopback aliases use this same component; remote aliases route to peer UIs.
    for address,port in [('127.0.0.1',80),(config['peers'][config['name']],PORT),('127.0.0.1',PORT)]:
        servers.append(ThreadingHTTPServer((address,port),handler(config)))
    class IPv6Server(ThreadingHTTPServer):
        address_family=socket.AF_INET6
        def server_bind(self):
            self.socket.setsockopt(socket.IPPROTO_IPV6,socket.IPV6_V6ONLY,1);super().server_bind()
    try:servers.append(IPv6Server(('::1',80),handler(config)))
    except OSError as error:
        # An existing LAN frontend may own [::]:80; IPv4 and direct LAN endpoints
        # remain independent. Its virtual host can forward aliases to loopback 8797.
        import errno
        if error.errno not in (errno.EADDRINUSE,errno.EAFNOSUPPORT):raise
    for server in servers:
        server.daemon_threads=True
        threading.Thread(target=server.serve_forever,daemon=True).start()
    print(json.dumps({'node':config['name'],'url':'http://jswarm.localhost/pipeline.html','port':PORT}),flush=True)
    try:threading.Event().wait()
    finally:
        for server in servers:server.shutdown();server.server_close()


IPV6_FRONTEND="""
    # JSwarm loopback names share an existing IPv6 frontend without LAN exposure.
    server {
        listen [::]:80;
        server_name jswarm.localhost *.jswarm.localhost;
        allow ::1;
        deny all;
        location / {
            proxy_pass http://127.0.0.1:8797;
            proxy_set_header Host $http_host;
            proxy_set_header Origin $http_origin;
            proxy_buffering off;
            proxy_read_timeout 60s;
        }
    }
"""


def frontend_configuration(source):
    if 'server_name jswarm.localhost *.jswarm.localhost;' in source:return source
    if 'server_name map.local' not in source or not source.rstrip().endswith('}'):
        raise ValueError('Unrecognized shared frontend configuration')
    end=source.rfind('}')
    return source[:end]+IPV6_FRONTEND+source[end:]


def configure_ipv6_frontend(base):
    # The existing stateless view deployer owns this nginx configuration. Keep its
    # other routes and assets, validate before reload, and restore on failure.
    result=subprocess.run(['docker','inspect','swarm-control-view'],capture_output=True,text=True)
    if result.returncode:return
    row=json.loads(result.stdout)[0]
    mounts={m['Destination']:m['Source'] for m in row.get('Mounts',[])}
    if '/etc/nginx/nginx.conf' not in mounts or '/usr/share/nginx/html' not in mounts:
        raise ValueError('Existing view ownership cannot be established')
    path=Path(mounts['/etc/nginx/nginx.conf']);before=path.read_text();after=frontend_configuration(before)
    if after==before:return
    backup=base/('nginx-before-'+hashlib.sha256(before.encode()).hexdigest()+'.conf')
    backup.write_text(before)
    # In-place write preserves the inode of nginx's read-only file bind mount.
    path.write_text(after)
    try:
        subprocess.run(['docker','exec','swarm-control-view','nginx','-t'],check=True,capture_output=True)
        subprocess.run(['docker','exec','swarm-control-view','nginx','-s','reload'],check=True,capture_output=True)
    except Exception:
        path.write_text(before)
        subprocess.run(['docker','exec','swarm-control-view','nginx','-s','reload'],capture_output=True)
        raise


def install(image,config):
    """Deploy a checksummed, independent UI image; never modify a node release."""
    image=json.loads(subprocess.check_output(['docker','image','inspect',image,'--format','{{json .Id}}'],text=True))
    if not re.fullmatch(r'sha256:[0-9a-f]{64}',image):raise ValueError('Immutable UI image required')
    encoded=json.dumps(config,sort_keys=True);revision=hashlib.sha256((image+encoded).encode()).hexdigest()
    base=Path.home()/'.local/share/jswarm-web';release=base/'releases'/revision;release.mkdir(parents=True,exist_ok=True)
    path=release/'config.json'
    if path.exists() and path.read_text()!=encoded:raise ValueError('Immutable web configuration changed')
    path.write_text(encoded);path.chmod(0o644)
    found=subprocess.run(['docker','inspect',CONTAINER],capture_output=True,text=True)
    current=json.loads(found.stdout)[0] if found.returncode==0 else None
    if current and (current['Config'].get('Labels') or {}).get(LABEL)!=SERVICE:raise ValueError('Web container belongs to another service')
    if current and (current['Config'].get('Labels') or {}).get('org.lifetech.revision')==revision and current['State']['Running']:
        return {'node':config['name'],'revision':revision,'image':image,'status':'current'}
    argv=['docker','run','-d','--name',CONTAINER,'--restart','unless-stopped','--network','host','--read-only','--cap-drop','ALL','--cap-add','NET_BIND_SERVICE','--security-opt','no-new-privileges','--memory','256m','--memory-swap','256m','--cpus','1','--pids-limit','128','--env','PYTHONDONTWRITEBYTECODE=1','--label',LABEL+'='+SERVICE,'--label','org.lifetech.revision='+revision,'--mount','type=bind,src='+str(path)+',dst=/run/jswarm-web.json,readonly','--log-driver','local','--log-opt','max-size=2m','--log-opt','max-file=2',
          '--health-cmd',"python3 -c \"import http.client; c=http.client.HTTPConnection('127.0.0.1',8797,timeout=3); c.request('GET','/health',headers={'Host':'jswarm.localhost'}); assert c.getresponse().status==200\"",'--health-interval','10s','--health-timeout','5s','--health-start-period','5s','--entrypoint','python3',image,'/opt/swarm-node/scripts/node_web.py','serve','--config','/run/jswarm-web.json']
    previous=CONTAINER+'-previous'
    if current:
        subprocess.run(['docker','rename',CONTAINER,previous],check=True,capture_output=True)
        subprocess.run(['docker','stop','--time','10',previous],check=True,capture_output=True)
    try:
        subprocess.run(argv,check=True,capture_output=True)
        deadline=time.monotonic()+30
        while True:
            try:
                c=http.client.HTTPConnection('127.0.0.1',PORT,timeout=2)
                c.request('GET','/health',headers={'Host':'jswarm.localhost'});r=c.getresponse()
                good=r.status==200 and json.loads(r.read()).get('node')==config['name'];c.close()
                if good:break
            except (OSError,ValueError,http.client.HTTPException):pass
            if time.monotonic()>deadline:raise RuntimeError('Node web did not become ready')
            time.sleep(.5)
        configure_ipv6_frontend(base)
    except Exception:
        subprocess.run(['docker','rm','-f',CONTAINER],capture_output=True)
        if current:
            subprocess.run(['docker','rename',previous,CONTAINER],check=True,capture_output=True)
            subprocess.run(['docker','start',CONTAINER],check=True,capture_output=True)
        raise
    if current:subprocess.run(['docker','rm',previous],check=True,capture_output=True)
    receipt={'node':config['name'],'image':image,'revision':revision,'status':'ready','url':'http://jswarm.localhost/pipeline.html'}
    pending=base/'deployment.tmp';pending.write_text(json.dumps(receipt));pending.replace(base/'deployment.json')
    return receipt


def deploy(image,settings,names=None):
    """Publish one UI image to existing enrolled machines through admin transport."""
    import shlex
    import sys
    root=Path(__file__).resolve().parents[2]
    sys.path[:0]=[str(root),str(root/'JSwarm/swarm-control/server'),str(root/'swarm-control/server')]
    from docker_utils.policy import inventory,ssh
    from docker_utils.images import ImageResolver
    from agent_client import AgentClient
    from agent_updates import deployment_lock
    config=configuration(settings)
    accounts={h['host']:h for h in inventory()}
    selected=names or list(config['peers'])
    if any(name not in config['peers'] for name in selected):raise ValueError('Use existing enrolled node names')
    hosts=[accounts[name] for name in selected if name!=config['name'] and name in accounts]
    if any(name!=config['name'] and name not in accounts for name in selected):raise ValueError('Enrolled owner SSH account unavailable; deploy from the coordinator')
    # Web listeners need the host network and low loopback port. Keep this
    # stateless service on system Docker even when compute uses rootless Docker.
    def admin_ssh(host):return ssh(host)+['env','-u','DOCKER_CONTEXT','DOCKER_HOST=unix:///var/run/docker.sock']
    resolver=ImageResolver(hosts,admin_ssh)
    source=Path(__file__).read_text()
    installer="""import hashlib,json,pathlib,sys
p=json.load(sys.stdin)
settings=json.loads((pathlib.Path.home()/'.config/lifetech-launcher/peers.json').read_text())
if settings['name']!=p['settings']['name'] or hashlib.sha256(pathlib.Path('/etc/machine-id').read_bytes()).hexdigest()!=p['machine']:raise ValueError('Enrolled node identity differs')
namespace={'__name__':'node_web_installer','__file__':str(pathlib.Path.home()/'node_web.py')}
exec(compile(p['source'],'node_web.py','exec'),namespace)
print(json.dumps(namespace['install'](p['image'],namespace['configuration'](p['settings']))))
"""
    rows=[]
    with deployment_lock():
        for name in selected:
            host=accounts.get(name) if name!=config['name'] else None
            client=AgentClient({'host':name,'agent_endpoint':'http://'+config['peers'][name]+':8790'})
            try:
                status=client.call('status',timeout=15)
                if status.get('node')!=name:raise ValueError('Enrolled node identity differs')
                resolver.ensure(image,[host])
                actual=resolver.host_images[name if host else 'local'][image]
                if host:
                    payload={'source':source,'settings':{**settings,'name':name},'image':actual,'machine':status['machine']}
                    result=subprocess.run(admin_ssh(host)+[shlex.join(['python3','-c',installer])],input=json.dumps(payload),capture_output=True,text=True,timeout=90)
                    if result.returncode:raise RuntimeError(result.stderr[-1500:])
                    row=json.loads(result.stdout)
                else:row=install(actual,config)
                rows.append(row);print(json.dumps(row),flush=True)
            except Exception as error:
                row={'node':name,'status':'failed','error':str(error)[:1500]};rows.append(row);print(json.dumps(row),flush=True)
                break  # One verified deployment at a time; leave later nodes untouched.
            finally:client.close()
        base=Path.home()/'.local/state/swarm-control/node-web';base.mkdir(parents=True,exist_ok=True)
        artifact={'image':image,'nodes':rows,'updated':time.time()}
        temporary=base/'deployment.tmp';temporary.write_text(json.dumps(artifact,indent=2));temporary.replace(base/'deployment.json')
    return len(rows)==len(selected) and all(row['status'] in ('ready','current') for row in rows)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command',choices=('serve','install','deploy','status'))
    parser.add_argument('--config',type=Path,default=Path.home()/'.config/lifetech-launcher/peers.json')
    parser.add_argument('--image',default='organization-swarm-node:1')
    parser.add_argument('--node',action='append',help='Deploy UI to selected enrolled nodes; default is the fleet')
    args=parser.parse_args()
    if args.command=='status':
        print((Path.home()/'.local/share/jswarm-web/deployment.json').read_text());return
    settings=json.loads(args.config.read_text())
    if args.command=='serve':serve(settings)
    elif args.command=='deploy':
        if not deploy(args.image,settings,args.node):raise SystemExit(1)
    else:print(json.dumps(install(args.image,configuration(settings))))


if __name__=='__main__':main()
