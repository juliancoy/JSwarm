"""Browser-approved native swarm enrollment; PKCE verifier never leaves this process."""
import base64
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import socket
import subprocess
import tempfile
import time
from urllib.parse import urlsplit
from urllib.request import Request,build_opener,HTTPRedirectHandler
import webbrowser


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):raise ValueError('Enrollment redirects are forbidden')


def origin_url(value):
    p=urlsplit(value)
    if p.scheme!='https' or not p.hostname or p.username or p.password or p.path or p.query or p.fragment:raise ValueError('Use a trusted HTTPS swarm origin')
    return value.rstrip('/')


def post(origin,path,body):
    request=Request(origin_url(origin)+path,json.dumps(body).encode(),{'Content-Type':'application/json','User-Agent':'JSwarm-Node/1.0'},method='POST')
    with build_opener(NoRedirect).open(request,timeout=15) as response:raw=response.read(65537)
    if len(raw)>65536:raise ValueError('Enrollment response exceeds limit')
    return json.loads(raw)


def local_address():
    rows=json.loads(subprocess.check_output(['ip','-j','-4','address'],text=True,timeout=5))
    for row in sorted(rows,key=lambda row:row['ifname'].startswith('wl')):
        if not row['ifname'].startswith(('en','eth','wl')):continue
        for entry in row.get('addr_info',[]):
            ip=ipaddress.IPv4Address(entry['local'])
            if entry.get('scope')=='global' and not ip.is_loopback and not ip.is_link_local:return str(ip)
    raise ValueError('Node needs an IPv4 address on Ethernet or Wi-Fi')


def save_config(path,config):
    path=Path(path);path.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
    with tempfile.NamedTemporaryFile(mode='w',dir=path.parent,delete=False) as handle:
        json.dump(config,handle);handle.flush();os.fsync(handle.fileno());temporary=Path(handle.name)
    temporary.chmod(0o600);temporary.replace(path)


def join(origin,name=None,config_path=None):
    from launcher_registration import register
    origin=origin_url(origin)
    path=Path(config_path or os.getenv('LIFETECH_PEER_CONFIG',str(Path.home()/'.config/lifetech-launcher/peers.json')))
    if path.exists():raise ValueError('This node is already provisioned; existing fleet membership will not be overwritten')
    machine=hashlib.sha256(Path('/etc/machine-id').read_bytes()).hexdigest()
    if name is None:name=re.sub('[^a-z0-9-]','-',socket.gethostname().lower().removesuffix('.local'))[:48]+'-'+machine[:8]+'.local'
    verifier=secrets.token_urlsafe(48);challenge=base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode()
    result=post(origin,'/api/node-enrollment/start',{'name':name,'machine':machine,'address':local_address(),
                'code_challenge':challenge,'code_challenge_method':'S256'})
    target=result['authorization_url']
    if urlsplit(target).scheme!='https' or urlsplit(target).netloc!=urlsplit(origin).netloc or urlsplit(target).path!='/join-node':raise ValueError('Invalid enrollment authorization destination')
    print('Sign in with PIdP and approve this node in your browser.\nNode: '+name+'\nMachine fingerprint: '+machine+'\n'+target,flush=True)
    webbrowser.open(target)
    deadline=time.monotonic()+min(600,result['expires_in'])
    while time.monotonic()<deadline:
        response=post(origin,'/api/node-enrollment/exchange',{'request_id':result['request_id'],'code_verifier':verifier})
        if response.get('status')=='approved':
            config=response['config']
            if config.get('name')!=name or config.get('master_api') or name not in config.get('peers',{}):raise ValueError('Enrollment identity differs')
            save_config(path,config)
            # Invitation/credentials remain owner-private and registration can
            # resume after an ambiguous network failure through its pending file.
            invitation=path.parent/'invitation.json'
            save_config(invitation,response['invitation'])
            try:register(invitation,path)
            except Exception:
                print('Provisioning saved privately. Internet registration is pending; retry the saved invitation with the launcher.',flush=True)
                return
            invitation.unlink(missing_ok=True)
            print('Node enrolled. Starting JSwarm; WAN connection readiness is reported by the launcher.',flush=True)
            return
        if response.get('status')!='authorization_pending':raise ValueError('Unexpected enrollment response')
        time.sleep(max(1,min(5,result.get('interval',3))))
    raise TimeoutError('Node sign-in expired; start a new join')
