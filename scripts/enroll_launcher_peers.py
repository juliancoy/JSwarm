"""Provision one private WebRTC fleet config via saved LAN SSH accounts."""
import json
from pathlib import Path
import secrets
import shlex
import subprocess
import sys
import socket
import re
import getpass
import ipaddress


def enroll(root):
    sys.path.insert(0, str(root / 'swarm-control/server'))
    sys.path.insert(0, str(root / 'scripts'))
    from launcher_peers import CONFIG
    inventory=Path.home()/'.local/state/orgportal/lan-ssh-devices.json'
    accounts=json.loads(inventory.read_text())['accounts']
    hosts=[]
    local=socket.gethostname().split('.')[0]+'.local'
    for name,user in accounts.items():
        if not re.fullmatch(r'[a-z0-9][a-z0-9-]*\.local',name) or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_.-]*',user):raise ValueError('Invalid saved provisioning account')
        if name!=local:hosts.append({'host':name,'user':user})
    hosts.append({'host':local,'user':getpass.getuser(),'local':True})
    def ssh(host):
        if host.get('local'):return ['bash','-c']
        return ['ssh','-i',str(Path.home()/'.ssh/orgportal_lan_ed25519'),'-o','BatchMode=yes','-o','IdentitiesOnly=yes','-o','StrictHostKeyChecking=yes','-o','ConnectTimeout=8',host['user']+'@'+host['host']]
    rows=json.loads(subprocess.check_output(['ip','-j','-4','addr','show','dev','enp7s0'],text=True))
    networks=[ipaddress.ip_network(a['local']+'/'+str(a['prefixlen']),strict=False) for row in rows for a in row.get('addr_info',[]) if a.get('scope')=='global']
    code="import json,subprocess; rows=json.loads(subprocess.check_output(['ip','-j','-4','addr'],text=True)); print(json.dumps([a['local'] for r in rows if r['ifname'].startswith(('en','eth','wl')) for a in r.get('addr_info',[]) if a.get('scope')=='global']))"
    peers={}
    for host in hosts:
        values=json.loads(subprocess.check_output(ssh(host)+['python3 -c '+shlex.quote(code)],text=True,timeout=20))
        address=next((value for value in values if any(ipaddress.ip_address(value) in network for network in networks)),None)
        if not address:raise RuntimeError('No authenticated Ethernet-LAN address for '+host['host'])
        peers[host['host']]=address
    local_host=next((host for host in hosts if host.get('local')),None)
    remote=next((host for host in hosts if not host.get('local')),None)
    if local_host and remote:
        with socket.socket(socket.AF_INET,socket.SOCK_DGRAM) as route:
            route.connect((peers[remote['host']],8790))
            peers[local_host['host']]=route.getsockname()[0]
    key = (json.loads(CONFIG.read_text()).get('key') if CONFIG.exists() else None) or secrets.token_hex(32)
    if len(bytes.fromhex(key)) != 32:
        raise ValueError('Existing mesh key must contain 32 bytes')
    # Persist locally first, so retrying a partially completed enrollment keeps its key.
    ordered = sorted(hosts, key=lambda host: not host.get('local', False))
    for host in ordered:
        config = {'name': host['host'], 'peers': peers, 'key': key}
        destination = '/home/' + host['user'] + '/.config/lifetech-launcher/peers.json'
        code = '''import pathlib,sys,os,json
os.umask(0o077)
p=pathlib.Path(DESTINATION);p.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
incoming=json.loads(sys.stdin.buffer.read());previous=json.loads(p.read_text()) if p.exists() else {};tmp=p.with_suffix('.tmp');tmp.write_text(json.dumps(previous|incoming));tmp.chmod(0o600);tmp.replace(p)
'''.replace('DESTINATION', repr(destination))
        subprocess.run(ssh(host) + ['python3 -c ' + shlex.quote(code)],
                       input=json.dumps(config).encode(), check=True, timeout=30)
        print('Enrolled ' + host['host'], flush=True)
    print('Enrollment complete. Recreate launcher containers with ./run.py --recreate-launcher on each machine to activate WebRTC.')
