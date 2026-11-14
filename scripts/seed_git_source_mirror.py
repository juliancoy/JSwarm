"""Seed the pinned private Harbor Genetics source through the signed node API.

Only repository objects are transferred; GitHub credentials stay on the controller.
This does not stage or activate a launcher. Run the normal fleet updater afterward.
"""
import argparse
import datetime
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import uuid

PROJECT = Path(__file__).resolve().parents[1]
WORKSPACE = PROJECT.parent
sys.path[:0] = [str(PROJECT), str(PROJECT / 'swarm-control/server')]


def install_code(commit, digest):
    return r'''import hashlib,json,os,pathlib,subprocess,sys,tempfile
expected=%r;commit=%r
config=pathlib.Path(os.environ.get('LIFETECH_PEER_CONFIG',str(pathlib.Path.home()/'.config/lifetech-launcher/peers.json')))
base=config.parent/'node-source';folder=base/'mirrors';folder.mkdir(parents=True,exist_ok=True,mode=0o700)
path=folder/('HarborGenetics-'+commit+'.bundle');h=hashlib.sha256()
fd,temporary=tempfile.mkstemp(prefix='harbor-',dir=folder)
try:
 with os.fdopen(fd,'wb') as out:
  while block:=sys.stdin.buffer.read(1024*1024):h.update(block);out.write(block)
  out.flush();os.fsync(out.fileno())
 if h.hexdigest()!=expected:raise RuntimeError('Git source bundle checksum differs')
 rows=subprocess.check_output(['git','bundle','list-heads',temporary],text=True)
 if commit not in {line.split()[0] for line in rows.splitlines()}:raise RuntimeError('Pinned commit missing from Git bundle')
 os.replace(temporary,path);path.chmod(0o600)
finally:
 if os.path.exists(temporary):os.unlink(temporary)
cache=base/'HarborGenetics.git'
if not cache.exists():subprocess.run(['git','init','--bare',str(cache)],check=True,capture_output=True)
mirror=folder/'HarborGenetics.git'
if not mirror.exists():subprocess.run(['git','init','--bare',str(mirror)],check=True,capture_output=True)
subprocess.run(['git','-C',str(mirror),'fetch','--no-tags','--',str(path),commit+':refs/mirrors/'+commit],check=True,capture_output=True,timeout=90)
subprocess.run(['git','-C',str(cache),'config','--unset-all','url.'+path.as_uri()+'.insteadOf'],capture_output=True)
key='url.'+str(mirror)+'.insteadOf'
subprocess.run(['git','-C',str(cache),'config','--replace-all',key,'https://github.com/juliancoy/HarborGenetics.git'],check=True)
r=subprocess.run(['git','-C',str(cache),'fetch','--no-tags','--','https://github.com/juliancoy/HarborGenetics.git',commit],capture_output=True,text=True,timeout=90)
if r.returncode:raise RuntimeError('Local source mirror verification failed: '+r.stderr[-500:])
actual=subprocess.check_output(['git','-C',str(cache),'rev-parse',commit+'^{commit}'],text=True).strip()
if actual!=commit:raise RuntimeError('Pinned Git commit differs')
print(json.dumps({'source_commit':actual,'bundle_sha256':expected,'local_git_mirror_verified':True}),flush=True)
''' % (digest,commit)


def main():
    from agent_client import AgentClient
    from agent_updates import LauncherUpdates
    from scripts.launcher_discovery import resolve_endpoint
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--node', action='append', required=True, dest='nodes')
    parser.add_argument('--bundle-cache', type=Path, default=Path.home()/'.cache/jswarm/git-source-mirrors')
    args = parser.parse_args()
    updater = LauncherUpdates.desired()
    source = updater.git_source['repositories']['HarborGenetics']
    commit = source['commit']
    repository = WORKSPACE/'HarborGenetics'
    folder = args.bundle_cache
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    bundle = folder/(commit+'.bundle')
    if not bundle.exists():
        ref = 'refs/deployment-mirrors/harbor-genetics-'+uuid.uuid4().hex
        subprocess.run(['git','-C',str(repository),'update-ref',ref,commit],check=True)
        try:
            subprocess.run(['git','-C',str(repository),'bundle','create',str(bundle),ref],check=True)
        finally:
            subprocess.run(['git','-C',str(repository),'update-ref','-d',ref],check=True)
    bundle.chmod(0o600)
    with bundle.open('rb') as stream:
        digest = hashlib.file_digest(stream,'sha256').hexdigest()
    code = install_code(commit,digest)
    for name in args.nodes:
        endpoint,_ = resolve_endpoint(name)
        host = {'host':name,'agent_endpoint':endpoint,'local':False}
        client = AgentClient(host)
        try:
            status = client.call('status',timeout=30)
        finally:
            client.close()
        with bundle.open('rb') as stream:
            result = subprocess.run(updater.command(host,code,status=status),stdin=stream,
                                    capture_output=True,text=True,timeout=300)
        record = {'date':datetime.datetime.now(datetime.timezone.utc).isoformat(),
                  'command':['python','-m','scripts.seed_git_source_mirror','--node',name],
                  'publication_revision':updater.revision,'source_commit':commit,
                  'bundle_sha256':digest,'result':result.returncode,
                  'code_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
        with Path(__file__).with_name(Path(__file__).name+'.test-log.jsonl').open('a') as log:
            log.write(json.dumps(record)+'\n')
        print(json.dumps(record),flush=True)
        if result.returncode:
            raise RuntimeError('Signed source mirror seeding failed: '+result.stderr[-1000:])
        print(result.stdout.strip(),flush=True)


if __name__ == '__main__':
    main()
