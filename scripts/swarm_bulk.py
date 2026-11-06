"""Default verified large-object routing over enrolled launcher control."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys

BASE = Path.home()/'.local/state/lifetech-launcher/bulk-transport'
DEFAULT_MIN_BYTES = 64 * 1024**2  # Routing preference, never an input-size limit.


class IntegrityError(RuntimeError):
    pass


def preferred(receipt, config=None):
    config = config or {}
    # Workflow-owned batches can explicitly prefer direct transport below the
    # general routing threshold; enrollment and checksum checks still apply.
    return receipt.get('prefer_bulk') is True or receipt['bytes'] >= config.get('min_bytes', DEFAULT_MIN_BYTES)


def try_copy(source, destination, receipt, target, metadata, execute):
    """Return transport name, or None for an encrypted launcher-pipe fallback.

    Missing/offline bulk peers are normal during rolling enrollment. Integrity
    rejection is terminal; never mask it as an unavailable transport.
    """
    path = BASE/'config.json'
    if not path.exists():return None
    config = json.loads(path.read_text())
    if not preferred(receipt, config):return None
    peers = config['peers']
    for host in (source, destination):
        peer = peers.get(host['host'])
        if not peer or peer['machine'] != host.get('machine'):return None
    source_root = peers[source['host']]['root']; destination_root = peers[destination['host']]['root']
    offer = ['python3',source_root+'/current/bulk.py','offer','--path',receipt['path'],
             '--sha256',receipt['sha256'],'--size',str(receipt['bytes']),
             '--peer',destination['host'],'--machine',source['machine']]
    result = subprocess.run(execute(source,offer),capture_output=True,text=True,timeout=30)
    if result.returncode == 78:raise IntegrityError('Bulk source identity or immutable receipt rejected')
    if result.returncode:return None
    obj = json.loads(result.stdout)
    receive = ['python3',destination_root+'/current/bulk.py','receive','--source',source['host'],
               '--machine',source['machine'],'--object',obj['object'],'--destination',target,
               '--sha256',receipt['sha256'],'--size',str(receipt['bytes']),'--metadata',metadata]
    # No total-duration limit: a valid large transfer may take hours.
    result = subprocess.run(execute(destination,receive),capture_output=True,text=True)
    if result.returncode == 78:raise IntegrityError('Bulk transfer checksum or enrolled identity rejected')
    return 'mtls-https' if result.returncode == 0 else None


def load_config():
    config = json.loads((BASE/'config.json').read_text())
    machine = hashlib.sha256(Path('/etc/machine-id').read_bytes()).hexdigest()
    if machine != config['machine']:raise ValueError('Bulk service machine identity changed')
    return config


def offer(config, path, sha256, size, peer, machine):
    import re
    if __package__:from .swarm_tls_transfer import atomic_json,identity
    else:from transfer import atomic_json,identity
    if machine != config['machine'] or peer not in config['peers']:raise ValueError('Unknown enrolled peer')
    if not re.fullmatch('[0-9a-f]{64}',sha256):raise ValueError('Invalid immutable digest')
    path = Path(path).resolve(strict=True)
    if not path.is_file() or path.stat().st_size != size:raise ValueError('Source size changed')
    obj = hashlib.sha256((str(path)+'\0'+sha256+'\0'+peer).encode()).hexdigest()
    row = {'path':str(path),'sha256':sha256,'identity':identity(path.stat()),
           'clients':[config['peers'][peer]['certificate_sha256']]}
    manifest=BASE/'objects'/(obj+'.json')
    # A warm offer already binds this exact file identity and enrolled client.
    # Avoid thousands of identical durable manifest writes on every listing.
    try:previous=json.loads(manifest.read_text())
    except (OSError,ValueError):previous=None
    if previous!=row:atomic_json(manifest,row)
    return {'object':obj}



def inbox(config,destination,peer,machine,job_key,files,max_bytes,expires):
    """Create an immutable output capability before submitting a node job."""
    import re,time
    if __package__:from .swarm_tls_transfer import atomic_json
    else:from transfer import atomic_json
    if machine!=config['machine'] or peer not in config['peers'] or not re.fullmatch('[a-f0-9]{64}',job_key):raise ValueError('Invalid output grant identity')
    if not isinstance(files,dict) or not files or any(not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,79}',n) or type(v) is not int or v<0 for n,v in files.items()):raise ValueError('Invalid output file limits')
    if type(max_bytes) is not int or max_bytes<1 or not time.time()<expires<=time.time()+30*86400:raise ValueError('Invalid output quota/expiry')
    destination=Path(destination).resolve();destination.mkdir(parents=True,exist_ok=True,mode=0o700)
    token=hashlib.sha256((str(destination)+'\0'+job_key+'\0'+peer).encode()).hexdigest()
    row={'destination':str(destination),'clients':[config['peers'][peer]['certificate_sha256']],
         'job_key':job_key,'files':files,'max_bytes':max_bytes,'expires':expires}
    path=BASE/'inboxes'/(token+'.json')
    if path.exists():
        prior=json.loads(path.read_text())
        if any(prior.get(k)!=v for k,v in row.items() if k!='expires'):raise ValueError('Output grant contract changed')
        row['expires']=max(prior['expires'],expires)
    atomic_json(path,row)
    return {'peer':config['name'],'machine':config['machine'],'grant':token,'job_key':job_key}

def main():
    import argparse
    import os
    import re
    from transfer import fetch
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='action',required=True)
    source = sub.add_parser('offer')
    source.add_argument('--path',required=True);source.add_argument('--peer',required=True)
    target = sub.add_parser('receive')
    for name in ['source','object','destination','metadata']:target.add_argument('--'+name,required=True)
    for cmd in (source,target):
        cmd.add_argument('--machine',required=True);cmd.add_argument('--sha256',required=True)
        cmd.add_argument('--size',type=int,required=True)
    sink=sub.add_parser('inbox')
    for name in ('destination','peer','machine','job-key','files'):sink.add_argument('--'+name,required=True)
    sink.add_argument('--max-bytes',type=int,required=True);sink.add_argument('--expires',type=float,required=True)
    args = parser.parse_args();config = load_config()
    if args.action=='inbox':
        print(json.dumps(inbox(config,args.destination,args.peer,args.machine,args.job_key,json.loads(args.files),args.max_bytes,args.expires)));return
    if args.action == 'offer':
        print(json.dumps(offer(config,args.path,args.sha256,args.size,args.peer,args.machine)));return
    peer = config['peers'].get(args.source)
    if not peer or peer['machine'] != args.machine or not re.fullmatch('[0-9a-f]{64}',args.object):
        raise ValueError('Unknown source identity or object')
    metadata = json.loads(args.metadata)
    if not re.fullmatch('[0-9a-f]{32}',metadata['id']):raise ValueError('Invalid progress identifier')
    fetch('https://'+peer['address']+':'+str(config['port'])+'/objects/'+args.object,
          args.destination,args.sha256,args.size,BASE/'ca.crt',BASE/'identity.crt',BASE/'identity.key',
          pinned_key=peer['public_key_pin'],metadata=metadata)


if __name__ == '__main__':
    try:main()
    except ValueError as error:raise SystemExit(78) from error
