"""Controller-owned Git delivery over enrolled, encrypted LAN transports."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import uuid

from scripts.node_git_source import git, validate


def bundle(repository, commit, destination):
    """Build a complete pinned bundle without changing a checkout or branch."""
    destination=Path(destination);destination.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    if not destination.exists():
        reference='refs/jswarm-delivery/'+uuid.uuid4().hex
        git(repository,'update-ref',reference,commit)
        try:
            with tempfile.TemporaryDirectory(dir=destination.parent) as temporary:
                path=Path(temporary)/'source.bundle'
                git(repository,'bundle','create',str(path),reference)
                path.chmod(0o600)
                try:os.link(path,destination)
                except FileExistsError:pass  # Another node preparation published this commit first.
        finally:git(repository,'update-ref','-d',reference)
    if commit not in {line.split()[0] for line in git(repository,'bundle','list-heads',str(destination)).splitlines()}:
        raise ValueError('Cached Git bundle has the wrong commit')
    with destination.open('rb') as stream:digest=hashlib.file_digest(stream,'sha256').hexdigest()
    return {'path':str(destination),'sha256':digest,'bytes':destination.stat().st_size,'prefer_bulk':True}


def deliver(updater, host, status, *, root=None):
    """Only the controller needs repository credentials; nodes fetch local bundles."""
    from agent_client import CONFIG
    from scripts.swarm_runtime import copy_verified_file
    from scripts.launcher_discovery import resolve_endpoint
    repositories=updater.git_source['repositories'];validate(repositories)
    root=Path(root) if root is not None else Path(__file__).resolve().parents[2]
    settings=json.loads(CONFIG.read_text())
    name=settings['name']
    machine=hashlib.sha256(Path('/etc/machine-id').read_bytes()).hexdigest()
    endpoint=resolve_endpoint(name)[0]
    source={'host':name,'machine':machine,'local':True,'agent_endpoint':endpoint}
    destination={**host,'local':False,'machine':status['machine'],
                 'agent_endpoint':host.get('agent_endpoint') or resolve_endpoint(host['host'])[0]}
    # Probe in the enrolled command owner, whose host paths may differ from the
    # node container paths. No saved IP address is used to select that owner.
    probe="""import json,os,pathlib,subprocess
p=pathlib.Path(os.environ.get('LIFETECH_PEER_CONFIG',str(pathlib.Path.home()/'.config/lifetech-launcher/peers.json')))
base=p.parent/'node-source';result={}
for name,commit in json.loads(%r).items():
 r=subprocess.run(['git','-C',str(base/(name+'.git')),'rev-parse','--verify',commit+'^{commit}'],capture_output=True,text=True)
 result[name]=r.returncode==0 and r.stdout.strip()==commit
print(json.dumps({'base':str(base),'cached':result}))
""" % json.dumps({name:spec['commit'] for name,spec in repositories.items()})
    result=subprocess.run(updater.command(host,probe,status=status),capture_output=True,text=True,timeout=60,check=True)
    remote=json.loads(result.stdout)
    if not Path(remote['base']).is_absolute():raise ValueError('Source cache must be absolute')
    destination['cache']=remote['base']+'/delivery'
    for name,spec in repositories.items():
        if remote['cached'].get(name) is True:continue
        if updater.stopped():raise InterruptedError('Source delivery stopped')
        repository=root if name=='Organization' else root/name
        # Missing controller objects are fetched by the controller, never nodes.
        try:git(repository,'cat-file','-e',spec['commit']+'^{commit}')
        except subprocess.CalledProcessError:
            git(repository,'-c','core.hooksPath=/dev/null','fetch','--no-tags','--',spec['origin'],spec['commit'])
        cache=Path.home()/'.cache/jswarm/source-bundles'/name/(spec['commit']+'.bundle')
        receipt=bundle(repository,spec['commit'],cache)
        transferred=copy_verified_file(source,destination,receipt,resume=True)
        # Local delivery needs no socket or copy; both run as the enrolled owner.
        code="""import os,pathlib
namespace={'__name__':'node_git_source'}
exec(compile(%r,'node_git_source.py','exec'),namespace)
p=pathlib.Path(os.environ.get('LIFETECH_PEER_CONFIG',str(pathlib.Path.home()/'.config/lifetech-launcher/peers.json')))
namespace['import_bundle'](p.parent/'node-source',%r,%r,%r,%r)
""" % (updater.git_source['installer'],name,spec['commit'],transferred['path'],receipt['sha256'])
        subprocess.run(updater.command(host,code,status=status),capture_output=True,text=True,timeout=180,check=True)
