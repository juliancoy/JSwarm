"""Publish and mount immutable objects without replacing existing filesystems.

Uses enrolled signed command execution, pinned bulk mTLS grants and existing
filesystem cache authorization. Accepted jobs retain their old mounts. Each
object/client release has its own immutable mount and service identity.
"""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys

PROJECT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(PROJECT), str(PROJECT/'swarm-control/server')]
from agent_client import AgentClient, CONFIG
from agent_updates import LauncherUpdates
from scripts.swarm_memory import validate

FILES = ('scripts/__init__.py', 'scripts/swarm_memory.py', 'scripts/swarm_memory_mount.py',
         'scripts/swarm_fs_mount.py', 'scripts/swarm_filesystem.py', 'scripts/run_input_cache.py',
         'scripts/swarm_immutable_read.py', 'scripts/launcher_discovery.py',
         'scripts/swarm_host_service.py', 'swarm-control/server/agent_client.py')

PUBLISH = r'''
import hashlib,json,pathlib,sys
p=json.load(sys.stdin)
if hashlib.sha256(pathlib.Path('/etc/machine-id').read_bytes()).hexdigest()!=p['machine']:raise ValueError('Origin machine differs')
namespace={};exec(p['memory_code'],namespace)
manifest=namespace['seal'](p['path'],p['sha256'],p['page_bytes'])
root=pathlib.Path.home()/'.local/state/lifetech-launcher/bulk-transport'
sys.path.insert(0,str(root/'current'));import bulk
cfg=bulk.load_config()
clients={name:bulk.offer(cfg,p['path'],manifest['sha256'],manifest['bytes'],name,p['machine'])['object'] for name in p['clients']}
source={'source':cfg['name'],'machine':cfg['machine'],'sha256':manifest['sha256'],'bytes':manifest['bytes'],'clients':clients}
print(json.dumps({'object':manifest,'sources':[source]}))
'''

INSTALL = r'''
import hashlib,json,os,pathlib,shlex,sys
p=json.load(sys.stdin);os.umask(0o077)
if hashlib.sha256(pathlib.Path('/etc/machine-id').read_bytes()).hexdigest()!=p['machine']:raise ValueError('Memory client machine differs')
home=pathlib.Path.home();settings=json.loads((home/'.config/lifetech-launcher/filesystem.json').read_text())
enrolled=json.loads((home/'.config/lifetech-launcher/peers.json').read_text())
if enrolled['name']!=p['node']:raise ValueError('Memory client name differs')
settings['local_gateway']=True
settings['peers']=[n for n in [settings['origin'],*settings.get('peers',[])] if n!=p['node'] and n in enrolled['peers']]
deps=home/'.local/share/swarm-filesystem/dependencies'
if not (deps/'mfusepy.py').exists():raise ValueError('Existing authenticated filesystem and FUSE installation required')
version=hashlib.sha256(json.dumps({'files':p['files'],'object':p['object'],'sources':p['sources'],'cache_bytes':p['cache_bytes'],'replicas':p['replicas']},sort_keys=True).encode()).hexdigest()
root=home/'.local/share/swarm-memory'/version;root.mkdir(parents=True,exist_ok=True)
for name,source in p['files'].items():
 target=root/name;target.parent.mkdir(parents=True,exist_ok=True)
 if target.exists() and target.read_text()!=source:raise ValueError('Immutable memory runtime changed')
 if not target.exists():target.write_text(source);target.chmod(0o400)
config={'object':p['object'],'sources':p['sources'],'filesystem':settings,'cache_bytes':p['cache_bytes'],'replicas':p['replicas']}
pending=root/'config.tmp';pending.write_text(json.dumps(config));pending.chmod(0o600);pending.replace(root/'config.json')
mount=home/'SwarmMemory'/p['object']['id']/version;mount.mkdir(parents=True,exist_ok=True)
unit='swarm-memory-'+version[:24]+'.service'
units=home/'.config/systemd/user';units.mkdir(parents=True,exist_ok=True)
command=shlex.join(['/usr/bin/python3',str(root/'scripts/swarm_memory_mount.py'),'--config',str(root/'config.json'),str(mount)])
text='[Unit]\nDescription=Shared immutable swarm memory\nAfter=network-online.target swarm-filesystem.service\n[Service]\nExecStart='+command+'\nEnvironment="PYTHONPATH='+str(deps)+'"\nEnvironment=PYTHONDONTWRITEBYTECODE=1\nUMask=0077\nRestart=on-failure\nRestartSec=5\nTimeoutStopSec=10\n[Install]\nWantedBy=default.target\n'
target=units/unit
if target.exists() and target.read_text()!=text:raise ValueError('Immutable memory service changed')
target.write_text(text)
namespace={};exec(p['files']['scripts/swarm_host_service.py'],namespace)
systemctl=namespace['systemctl'];systemctl('daemon-reload',check=True,capture_output=True)
# The host-service bridge uses a narrow container namespace on some nodes;
# systemctl --now incorrectly treats that namespace as a non-running systemd.
# Explicit start still talks to the enrolled owner's real user bus.
systemctl('enable',unit,check=True,capture_output=True)
systemctl('start',unit,check=True,capture_output=True)
print(json.dumps({'node':p['node'],'machine':p['machine'],'object_id':p['object']['id'],'revision':version,'path':str(mount/'object'),'metrics':str(mount/'metrics.json'),'unit':unit}))
'''


def execute(name, code, payload, timeout=90):
    cfg = json.loads(CONFIG.read_text())
    if name not in cfg['peers']: raise ValueError('Use an enrolled hostname')
    host = {'host': name, 'local': name == cfg['name'], 'agent_route':'auto'}
    client=AgentClient(host)
    try: status=client.call('status',timeout=15)
    finally: client.close()
    if status['node']!=name: raise ValueError('Enrolled node identity differs')
    host['machine']=status['machine']
    updater=LauncherUpdates.__new__(LauncherUpdates)
    result=subprocess.run(updater.command(host,code),input=json.dumps({**payload,'machine':status['machine'],'node':name}),
                          capture_output=True,text=True,timeout=timeout)
    if result.returncode: raise RuntimeError(name+': '+result.stderr[-1000:])
    return json.loads(result.stdout)


def publish(source, path, sha256, clients, *, page_bytes=65536):
    cfg=json.loads(CONFIG.read_text())
    if not clients or any(n not in cfg['peers'] for n in clients): raise ValueError('Use enrolled memory clients')
    result=execute(source,PUBLISH,{'path':str(path),'sha256':sha256,'clients':clients,'page_bytes':page_bytes,
                                 'memory_code':(PROJECT/'scripts/swarm_memory.py').read_text()},timeout=600)
    validate(result['object'])
    return result


def deploy(publication, names, *, cache_bytes=256*1024**2, replicas=2):
    validate(publication['object'])
    if type(cache_bytes) is not int or not 4*1024**2<=cache_bytes<=1024**3: raise ValueError('Cache budget must be 4 MiB..1 GiB')
    if type(replicas) is not int or not 0<=replicas<=3: raise ValueError('Use zero to three opportunistic peer replicas')
    files={name:(PROJECT/name).read_text() for name in FILES}
    results={}
    for name in names:
        try:
            results[name]=execute(name,INSTALL,{**publication,'files':files,'cache_bytes':cache_bytes,'replicas':replicas})
        except (OSError,RuntimeError,ValueError,subprocess.SubprocessError) as error:
            results[name]={'ready':False,'reason':str(error)[-1000:]}
    return results


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source',required=True);parser.add_argument('--path',required=True)
    parser.add_argument('--sha256',required=True);parser.add_argument('--node',action='append')
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args();names=args.node or list(json.loads(CONFIG.read_text())['peers'])
    publication=publish(args.source,args.path,args.sha256,names)
    args.output.parent.mkdir(parents=True,exist_ok=True)
    metadata=args.output.with_name(args.output.stem+'.publication.json').resolve()
    metadata.write_text(json.dumps(publication));metadata.chmod(0o600)
    result={'publication':str(metadata),'object_id':publication['object']['id'],'nodes':deploy(publication,names)}
    args.output.write_text(json.dumps(result));args.output.chmod(0o600)
    print(json.dumps({'object':result['object_id'],'nodes':result['nodes']}))
