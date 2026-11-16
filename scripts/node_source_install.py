"""Activate controller-delivered source in the enrolled, bind-mounted node.

Executed through the existing signed owner command service. Requires only Python,
Git and Docker already present on the host; never builds or installs dependencies.
"""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys


def owned_node(config):
    """Locate the node and its actual supervisor through attested bind mounts."""
    probe="""import json,pathlib
paths=set()
for proc in pathlib.Path('/proc').glob('[0-9]*'):
 try:
  args=[a.decode() for a in (proc/'cmdline').read_bytes().split(bytes([0])) if a]
  for arg in args[1:3]:
   if pathlib.Path(arg).name=='launcher_peer_supervisor.py':paths.add(str(pathlib.Path(arg).resolve()))
 except (OSError,UnicodeError):pass
print(json.dumps(sorted(paths)))
"""
    for docker in (['docker'],['docker','--host','unix:///var/run/docker.sock']):
        result=subprocess.run(docker+['inspect','swarm-node'],capture_output=True,text=True,timeout=15)
        if result.returncode:continue
        container=json.loads(result.stdout)[0]
        mounts=container.get('Mounts',[])
        if not any(m['Type']=='bind' and m['Destination'] in ('/run/launcher-config',str(config.parent))
                   and Path(m['Source']).resolve()==config.parent.resolve() and m['RW'] for m in mounts):continue
        result=subprocess.run(docker+['exec',container['Id'],'python3','-c',probe],
                              capture_output=True,text=True,check=True,timeout=20)
        paths=json.loads(result.stdout)
        if len(paths)!=1:raise RuntimeError('Expected one running node supervisor')
        running=Path(paths[0])
        for mount in sorted(mounts,key=lambda m:len(m['Destination']),reverse=True):
            target=Path(mount['Destination'])
            if not running.is_relative_to(target):continue
            if mount['Type']!='bind':
                raise RuntimeError('Node supervisor requires a host source bind mount')
            bootstrap=Path(mount['Source'])/running.relative_to(target)
            if not bootstrap.is_file():raise RuntimeError('Mounted supervisor is unavailable to command owner')
            return docker,container['Id'],bootstrap
        raise RuntimeError('Node supervisor is not host-mounted; recreate with the standard runner')
    raise RuntimeError('No node container with this enrolled configuration mount')


def install(payload):
    config=Path(os.environ.get('LIFETECH_PEER_CONFIG',str(Path.home()/'.config/lifetech-launcher/peers.json')))
    if hashlib.sha256(Path('/etc/machine-id').read_bytes()).hexdigest()!=payload['machine']:
        raise ValueError('Update machine identity changed')
    settings=json.loads(config.read_text())
    if settings['name']!=payload['node'] or settings.get('controller_public_key')!=payload['controller_public_key']:
        raise ValueError('Update enrollment identity changed')
    source=payload['git_source']
    if source.get('mode')!='mounted-v1':raise ValueError('Mounted source publication required')
    supervisor=payload['supervisor_source']
    if hashlib.sha256(supervisor.encode()).hexdigest()!=payload['supervisor_sha256']:
        raise ValueError('Supervisor checksum mismatch')
    compile(supervisor,'launcher_peer_supervisor.py','exec')
    namespace={'__name__':'node_git_source'}
    exec(compile(source['installer'],'node_git_source.py','exec'),namespace)
    workspace=namespace['workspace'](source['repositories'],config.parent/'node-source')
    # Source and bootstrap must come from the same committed publication.
    project=workspace/'JSwarm/scripts'
    if ((project/'launcher_peer_supervisor.py').read_text()!=supervisor
            or (project/'node_git_source.py').read_text()!=source['installer']
            or (project/'node_source_install.py').read_text()!=source['activation']):
        raise ValueError('Bootstrap differs from pinned Git source')
    for name,content in payload['files'].items():
        if name not in ('launcher_peers.py','launcher_discovery.py','launcher_registration.py',
                        'launcher_agent.py','launcher_registry.py'):
            raise ValueError('Unexpected node source file')
        if (project/name).read_text()!=content:raise ValueError('Source differs from publication')
    bootstrap=config.parent/'node-bootstrap/launcher_peer_supervisor.py'
    bootstrap.parent.mkdir(exist_ok=True,mode=0o700)
    targets=[bootstrap]
    docker=container=None
    if not payload['supervisor_supports_source'] or not bootstrap.is_file():
        docker,container,entry=owned_node(config)
        if entry.resolve().is_relative_to((config.parent/'node-source').resolve()):
            raise RuntimeError('Standard supervisor bootstrap must be outside managed source')
        targets.append(entry)
    # Once migrated, the enrolled owner can update the mounted bootstrap without
    # access to the node daemon; worker jobs may use a separate rootless daemon.
    for target in targets:
        temporary=target.with_suffix('.update.tmp')
        temporary.write_text(supervisor);temporary.chmod(0o600);temporary.replace(target)
    namespace['stage_source'](source['repositories'],config,payload['revision'])
    if not payload['supervisor_supports_source']:
        # One-time migration of the old supervisor. Only the node container is
        # restarted; Docker job containers and durable owner commands survive.
        subprocess.run(docker+['restart',container],check=True,capture_output=True,timeout=90)
    print(json.dumps({'revision':payload['revision'],'mode':'mounted-v1'}),flush=True)


if __name__=='__main__':
    install(json.load(sys.stdin))
