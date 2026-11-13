"""Install private filesystem configuration and mounts through standard node deployment."""
import json
from pathlib import Path
import subprocess
import sys
import time

PROJECT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(PROJECT), str(PROJECT/'swarm-control/server')]
from agent_client import AgentClient, CONFIG
from agent_updates import LauncherUpdates
from scripts.launcher_discovery import resolve_endpoint, attest_addresses

PLAN = Path.home()/'.config/map-master/filesystem-plan.json'
FILES = ('scripts/__init__.py', 'scripts/swarm_filesystem.py', 'scripts/swarm_fs_mount.py',
         'scripts/run_input_cache.py', 'scripts/launcher_discovery.py',
         'swarm-control/server/agent_client.py', 'scripts/swarm_host_service.py')

INSTALL = r'''
import hashlib,json,os,pathlib,subprocess,sys
p=json.load(sys.stdin);os.umask(0o077)
if hashlib.sha256(pathlib.Path('/etc/machine-id').read_bytes()).hexdigest()!=p['machine']:raise ValueError('Filesystem machine changed')
home=pathlib.Path.home();config=home/'.config/lifetech-launcher'
settings=p['settings']
if settings.get('root'):
 root=pathlib.Path(settings['root'])
 if not root.is_absolute() or not root.is_relative_to(home/'DNA') or '..' in root.parts:raise ValueError('Export must be inside the owner DNA directory')
 for item in [home/'DNA',root]:
  if item.is_symlink():raise ValueError('Export root cannot be a symlink')
  item.mkdir(parents=True,exist_ok=True,mode=0o700)
revision=hashlib.sha256(json.dumps(p['files'],sort_keys=True).encode()).hexdigest()
release=home/'.local/share/swarm-filesystem'/revision;release.mkdir(parents=True,exist_ok=True)
settings['client_release']=str(release);settings['local_gateway']=True
pending=config/'filesystem.tmp';pending.write_text(json.dumps(settings));pending.chmod(0o600);pending.replace(config/'filesystem.json')
for name,source in p['files'].items():
 target=release/name;target.parent.mkdir(parents=True,exist_ok=True)
 if target.exists() and target.read_text()!=source:raise ValueError('Immutable filesystem client changed')
 target.write_text(source)
if not settings.get('fuse',True):
 unit=home/'.config/systemd/user/swarm-filesystem.service'
 if unit.exists():
  namespace={};exec(p['files']['scripts/swarm_host_service.py'],namespace)
  namespace['systemctl']('disable','--now',unit.name,check=True,capture_output=True)
 print(json.dumps({'active':True,'fuse':False,'mounted':False,'revision':revision}))
 raise SystemExit(0)
# Only the small mount adapter is installed here. Bulk scientific data stays in RAM.
deps=home/'.local/share/swarm-filesystem/dependencies'
if not (deps/'mfusepy-3.1.1.dist-info').is_dir():
 deps.mkdir(parents=True,exist_ok=True)
 requirements=deps/'requirements.txt'
 requirements.write_text('mfusepy==3.1.1 --hash=sha256:69fb70cfc7f7cce595e6ff586f8451d8298f01f18286a157f24f564b24ec5a37\n')
 subprocess.run([sys.executable,'-m','pip','install','--disable-pip-version-check','--no-cache-dir','--no-deps','--only-binary=:all:','--require-hashes','--target',str(deps),'-r',str(requirements)],check=True,stdout=subprocess.DEVNULL)
mountpoint=home/'SwarmDNA';mountpoint.mkdir(exist_ok=True)
unit=home/'.config/systemd/user/swarm-filesystem.service';unit.parent.mkdir(parents=True,exist_ok=True)
import shlex
execstart=shlex.join(['/usr/bin/python3',str(release/'scripts/swarm_fs_mount.py'),str(mountpoint),'--foreground'])
text='[Unit]\nDescription=Private swarm filesystem backed by Latitude\nAfter=network-online.target\n[Service]\nType=simple\nEnvironment="PYTHONPATH='+str(deps)+'"\nEnvironment=PYTHONDONTWRITEBYTECODE=1\nExecStart='+execstart+'\nRestart=on-failure\nRestartSec=10\nTimeoutStopSec=20\nUMask=0077\n[Install]\nWantedBy=default.target\n'
changed=not unit.exists() or unit.read_text()!=text
unit.write_text(text)
namespace={};exec(p['files']['scripts/swarm_host_service.py'],namespace);systemctl=namespace['systemctl']
systemctl('daemon-reload',check=True,capture_output=True)
if changed:systemctl('try-restart',unit.name,check=True,capture_output=True)
systemctl('enable','--now',unit.name,check=True,capture_output=True)
active=systemctl('is-active','--quiet',unit.name).returncode==0
print(json.dumps({'active':active,'mountpoint':str(mountpoint),'revision':revision}))
'''


def deploy_fleet(names=None):
    if not PLAN.exists():return {}
    plan = json.loads(PLAN.read_text())
    if type(plan.get('fuse', True)) is not bool:raise ValueError('fuse must be true or false')
    enrolled = json.loads(CONFIG.read_text())
    if plan['origin'] not in enrolled['peers']:raise ValueError('Unenrolled filesystem origin')
    files = {name: (PROJECT/name).read_text() for name in FILES}
    result = {}
    for name in names or enrolled['peers']:
        client = None
        try:
            endpoint, addresses = resolve_endpoint(name)
            host = {'host': name, 'local': name == enrolled['name'], 'agent_endpoint': endpoint, 'resolved_addresses': addresses}
            client = AgentClient(host);status = client.call('status', timeout=5)
            attest_addresses(host, status);host['machine'] = status['machine']
            settings = {k: v for k, v in plan.items() if k not in ('root', 'fuse_library')}
            settings.setdefault('fuse', True)
            settings['peers'] = [n for n in enrolled['peers'] if n != name and n != plan['origin']]
            if name == plan['origin']:settings['root'] = plan['root']
            payload = {'machine': host['machine'], 'settings': settings, 'files': files}
            updater = LauncherUpdates.__new__(LauncherUpdates)
            completed = subprocess.run(updater.command(host, INSTALL), input=json.dumps(payload),
                                       capture_output=True, text=True, timeout=180)
            if completed.returncode:raise RuntimeError(completed.stderr[-700:])
            result[name] = json.loads(completed.stdout)
            from scripts.swarm_filesystem import request
            # Start/attach the capsule and its stable local gateway socket.
            check = client.call('filesystem.rpc', request(settings, 'status'), timeout=45)
            if check.get('result', {}).get('namespace') != settings['namespace']:raise RuntimeError('Filesystem namespace verification failed')
            if not settings['fuse']:continue
            deadline = time.monotonic()+40
            while True:
                mounted = client.call('filesystem.rpc', request(settings, 'mount.status'), timeout=10)
                if mounted.get('result', {}).get('mounted'):
                    result[name]['mounted'] = True;break
                if time.monotonic() > deadline:raise RuntimeError('Filesystem service has not produced its enrolled FUSE mount; inspect swarm-filesystem.service')
                time.sleep(.5)
        except Exception as error:result[name] = {'active': False, 'reason': str(error)[:800]}
        finally:
            if client:client.close()
    return result


if __name__ == '__main__':print(json.dumps(deploy_fleet(), indent=2))
