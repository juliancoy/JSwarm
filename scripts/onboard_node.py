"""Enroll one mDNS LAN node and install its supervised user test agent."""
import argparse
import hashlib
import ipaddress
import json
from pathlib import Path
import re
import runpy
import shlex
import subprocess
import sys
import time

ROOT = (Path(__file__).resolve().parents[2] if Path(__file__).resolve().parents[1].name in ('GenomeHarbor', 'JSwarm') else Path(__file__).resolve().parents[1])
FILES = ('launcher_peers.py', 'launcher_discovery.py', 'launcher_registration.py',
         'launcher_agent.py', 'launcher_registry.py', 'launcher_peer_supervisor.py',
         'launcher_update.py','swarm_node.py')


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_suffix('.tmp')
    temporary.touch(mode=0o600)
    temporary.chmod(0o600)
    temporary.write_text(json.dumps(value, indent=2) + '\n')
    temporary.replace(path)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('node', help='user@hostname.local')
    parser.add_argument('--address', help='SSH address override, including an IPv6 interface scope')
    args = parser.parse_args()
    if not re.fullmatch(r'[a-zA-Z_][a-zA-Z0-9_.-]*@[a-zA-Z0-9][a-zA-Z0-9-]*\.local', args.node):
        parser.error('Use user@hostname.local')
    user, node = args.node.split('@')
    node = node.lower()
    ssh = ['ssh', '-C', '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes',
           '-o', 'ConnectTimeout=8', '-o', 'ForwardAgent=no', '-o', 'HostKeyAlias=' + node]
    key = Path.home() / '.ssh/orgportal_lan_ed25519'
    if key.exists():
        ssh += ['-i', str(key)]
    if args.address:
        ssh += ['-o', 'HostName=' + args.address.replace('%', '%%')]
    ssh += [user + '@' + node]

    def remote(code, payload=None, timeout=40):
        return subprocess.run(ssh + ['python3 -c ' + shlex.quote(code)],
                              input=json.dumps(payload) if payload is not None else None,
                              capture_output=True, text=True, check=True, timeout=timeout).stdout

    config_path = Path.home() / '.config/lifetech-launcher/peers.json'
    config = json.loads(config_path.read_text())
    probe = json.loads(remote('''import hashlib,json,pathlib,socket,subprocess
print(json.dumps(dict(host=socket.gethostname().lower()+'.local',home=str(pathlib.Path.home()),
 machine=hashlib.sha256(pathlib.Path('/etc/machine-id').read_bytes()).hexdigest(),
 interfaces=json.loads(subprocess.check_output(['ip','-j','-4','addr'],text=True)),
 docker=json.loads(subprocess.check_output(['docker','info','--format','{{json .}}'],text=True))['OSType'],
 endpoint=subprocess.check_output(['docker','context','inspect','--format','{{.Endpoints.docker.Host}}'],text=True).strip())))
'''))
    if probe['host'] != node or probe['docker'] != 'linux':
        raise RuntimeError('Remote hostname or Docker platform does not match the requested node')
    rows = json.loads(subprocess.check_output(['ip', '-j', '-4', 'addr', 'show', 'dev', 'enp7s0'], text=True))
    networks = [ipaddress.ip_network(a['local'] + '/' + str(a['prefixlen']), strict=False)
                for row in rows for a in row.get('addr_info', []) if a.get('scope') == 'global']
    interfaces = sorted(probe['interfaces'], key=lambda row: row['ifname'].startswith('wl'))
    address = next((a['local'] for row in interfaces if row['ifname'].startswith(('en', 'eth', 'wl'))
                    for a in row.get('addr_info', []) if a.get('scope') == 'global'
                    and any(ipaddress.ip_address(a['local']) in net for net in networks)), None)
    if not address:
        raise RuntimeError('Node has no physical address on the coordinator Ethernet subnet')
    if node not in config['peers'] and len(config['peers']) >= 24:
        raise RuntimeError('Fleet already has 24 nodes')
    if any(name != node and value == address for name, value in config['peers'].items()):
        raise RuntimeError('Address is already enrolled under another node name')
    if not probe['endpoint'].startswith('unix://'):
        raise RuntimeError('Node needs a local Unix Docker socket')
    print(f'Verified {node} at {address}; Docker Linux available', flush=True)
    # Keep the existing fleet credential and all existing peers.
    config['peers'][node] = address
    destination = probe['home'] + '/.local/share/organization-node'
    remote_config = {**config, 'name': node}
    monitor=ROOT/'ramtop/ramtop'
    sources={name:(ROOT/'scripts'/name).read_text() for name in FILES}
    sources['launcher_agent.py'] += '\nPERFORMANCE_RUNTIME_B64 = '+repr(runpy.run_path(str(monitor/'bundle.py'))['encoded_runtime'](monitor))+'\n'
    sources['launcher_agent.py'] += '\nSTREAM_RUNTIME_SOURCE = '+repr((ROOT/'scripts/launcher_streams.py').read_text())+'\n'
    payload = dict(config=remote_config, folder=destination,
                   files=sources,
                   public_key=key.with_suffix('.pub').read_text().strip() if key.with_suffix('.pub').exists() else '')
    remote('''import json,os,pathlib,sys
os.umask(0o077)
v=json.load(sys.stdin)
p=pathlib.Path.home()/'.config/lifetech-launcher/peers.json';p.parent.mkdir(parents=True,exist_ok=True)
old=json.loads(p.read_text()) if p.exists() else {}
if old and (old['name']!=v['config']['name'] or old['key']!=v['config']['key']):
 raise RuntimeError('Node already belongs to a different fleet')
root=pathlib.Path(v['folder']);scripts=root/'scripts';scripts.mkdir(parents=True,exist_ok=True)
for name,source in v['files'].items():
 compile(source,name,'exec');(scripts/name).write_text(source)
t=p.with_suffix('.tmp');t.write_text(json.dumps(old|v['config']));t.chmod(0o600);t.replace(p)
if v['public_key']:
 p=pathlib.Path.home()/'.ssh/authorized_keys';p.parent.mkdir(parents=True,exist_ok=True)
 old=p.read_text() if p.exists() else ''
 if v['public_key'] not in old.splitlines():p.write_text(old.rstrip()+'\\n'+v['public_key']+'\\n');p.chmod(0o600)
''', payload)
    print('Installing supervised user agent and Python dependencies', flush=True)
    remote('''import json,os,pathlib,subprocess,sys
v=json.load(sys.stdin);root=pathlib.Path(v['folder']);venv=root/'venv'
subprocess.run(['python3','-m','venv',str(venv)],check=True)
subprocess.run([str(venv/'bin/pip'),'install','--disable-pip-version-check',
 'docker>=7,<8','cryptography>=43','aiortc>=1.14,<2','aiohttp>=3.12,<4','zeroconf>=0.151,<1'],check=True)
unit=pathlib.Path.home()/'.config/systemd/user/organization-test-agent.service'
unit.parent.mkdir(parents=True,exist_ok=True)
# Quote paths as systemd arguments, and escape literal percent specifiers.
def quote(value):return json.dumps(str(value).replace('%','%%'))
text='[Unit]\\nDescription=Swarm Node — discovery, admission and Docker workers\\nAfter=network-online.target\\n\\n[Service]\\n'
text+='ExecStart='+quote(venv/'bin/python')+' '+quote(root/'scripts/launcher_peer_supervisor.py')+'\\n'
text+='Environment='+quote('DOCKER_HOST='+v['endpoint'])+'\\n'
text+='Environment='+quote('LIFETECH_PEER_CONFIG='+str(pathlib.Path.home()/'.config/lifetech-launcher/peers.json'))+'\\n'
text+='Environment='+quote('LIFETECH_JOB_ROOT='+str(root/'.runtime/launcher-jobs'))+'\\n'
text+='Environment=PYTHONUNBUFFERED=1\\nRestart=on-failure\\nRestartSec=5\\nUMask=0077\\n\\n[Install]\\nWantedBy=default.target\\n'
changed=not unit.exists() or unit.read_text()!=text
unit.write_text(text)
subprocess.run(['loginctl','enable-linger'],check=True)
subprocess.run(['systemctl','--user','daemon-reload'],check=True)
subprocess.run(['systemctl','--user','enable','organization-test-agent.service'],check=True)
active=subprocess.run(['systemctl','--user','is-active','--quiet','organization-test-agent.service']).returncode==0
if not active:subprocess.run(['systemctl','--user','start','organization-test-agent.service'],check=True)
elif changed:raise RuntimeError('Agent service configuration changed; restart explicitly after active jobs finish')
''', dict(folder=destination, endpoint=probe['endpoint']), timeout=600)
    save(config_path, config)
    inventory_path = Path.home() / '.local/state/orgportal/lan-ssh-devices.json'
    inventory = json.loads(inventory_path.read_text()) if inventory_path.exists() else {}
    inventory.setdefault('accounts', {})[node] = user
    save(inventory_path, inventory)
    sys.path.insert(0, str(ROOT / 'swarm-control/server'))
    from agent_client import AgentClient
    client = AgentClient(dict(host=node, agent_endpoint='http://' + address + ':8790'))
    expected = hashlib.sha256(''.join(payload['files'][name] for name in FILES[:5]).encode()).hexdigest()
    deadline = time.monotonic() + 60
    try:
        while True:
            try:
                status = client.call('status', timeout=5)
                if status['node'] != node or status['machine'] != probe['machine']:
                    raise RuntimeError('Agent identity does not match the SSH machine')
                if status['ready'] and status['source_revision'] == expected and address in status['addresses']:
                    break
            except (OSError, ConnectionError):
                pass
            if time.monotonic() >= deadline:
                raise RuntimeError('Agent did not become ready; inspect journalctl --user -u organization-test-agent on the node')
            time.sleep(1)
    finally:
        client.close()
    print(f'Enrolled {node}: authenticated agent ready at {address}:8790', flush=True)
    # Mesh membership must also reach existing nodes; offline peers remain enrolled.
    for peer, peer_address in config['peers'].items():
        if peer in (node, config['name']):
            continue
        peer_user = inventory['accounts'].get(peer)
        if not peer_user:
            print(f'Fleet config pending on {peer}: no saved SSH account', flush=True)
            continue
        command = ['ssh', '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes',
                   '-o', 'ConnectTimeout=5', '-o', 'ForwardAgent=no',
                   '-o', 'HostName=' + peer_address, '-o', 'HostKeyAlias=' + peer]
        if key.exists():
            command += ['-i', str(key)]
        code = '''import json,pathlib,sys
p=pathlib.Path.home()/'.config/lifetech-launcher/peers.json'
old=json.loads(p.read_text());v=json.load(sys.stdin)
if old['name']!=v['name'] or old['key']!=v['key']:raise RuntimeError('Fleet identity mismatch')
t=p.with_suffix('.tmp');t.touch(mode=0o600);t.chmod(0o600);t.write_text(json.dumps(old|v));t.replace(p)
'''
        try:
            subprocess.run(command + [peer_user + '@' + peer, 'python3 -c ' + shlex.quote(code)],
                           input=json.dumps({**config, 'name': peer}), text=True,
                           capture_output=True, check=True, timeout=15)
            print('Updated fleet membership on ' + peer, flush=True)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
            print('Fleet config pending on ' + peer + ': SSH unavailable; rerun onboarding when online', flush=True)


if __name__ == '__main__':
    main()
