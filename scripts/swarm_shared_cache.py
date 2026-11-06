"""One Docker-owned, verified RAM volume per immutable object set and node.

The existing resource pool charges its owner once. Consumers mount the volume
read-only and reserve only private memory. No host IPC or payload disk fallback.
"""
import contextlib
import fcntl
import hashlib
import json
import math
from pathlib import Path
import re
import subprocess
import tempfile

from scripts.swarm_runtime import AgentClient,CapacityWait,shell_command

OWNER='org.lifetech.cache-owner'
DEPENDENCY='org.lifetech.shared-cache'


def contract(image,files):
    if not re.fullmatch(r'sha256:[0-9a-f]{64}',image):raise ValueError('Cache requires a pinned image')
    records={}
    for name,row in files.items():
        if not re.fullmatch(r'[A-Za-z0-9_.-]{1,120}',name) or name.startswith('.'):raise ValueError('Invalid cache filename')
        if type(row.get('bytes')) is not int or row['bytes']<1 or not re.fullmatch(r'[0-9a-f]{64}',row.get('sha256','')):raise ValueError('Invalid immutable cache object')
        records[name]={k:row[k] for k in ('bytes','sha256')}
    if not records:raise ValueError('Empty cache')
    value={'image':image,'files':records}
    key=hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':')).encode()).hexdigest()
    return key,value


def command(host,args,**kwargs):
    result=subprocess.run(shell_command(host,args),capture_output=True,text=True,timeout=kwargs.pop('timeout',45),**kwargs)
    if result.returncode:raise RuntimeError('Shared RAM cache command failed: '+result.stderr[-350:])
    return result.stdout


def inspect(host,kind,name):
    result=subprocess.run(shell_command(host,['docker',kind,'inspect',name]),capture_output=True,text=True,timeout=30)
    if result.returncode:
        if 'no such' in result.stderr.lower() or 'not found' in result.stderr.lower():return None
        raise RuntimeError('Cannot inspect shared cache: '+result.stderr[-250:])
    return json.loads(result.stdout)[0]


def validate_volume(row,cache):
    options=row.get('Options') or {}
    if (row.get('Driver')!='local' or options.get('type')!='tmpfs' or options.get('device')!='tmpfs'
            or (row.get('Labels') or {}).get(OWNER)!=cache['key']
            or 'size='+str(cache['shm_mb'])+'m' not in options.get('o','').split(',')):
        raise ValueError('Cache volume is not the owned RAM-only volume')


SEAL=r'''
import hashlib,json,pathlib,sys,os
v=json.loads(sys.argv[1]);root=pathlib.Path('/dev/shm');identities={}
for name,expected in v['files'].items():
 p=root/name;h=hashlib.sha256()
 with p.open('rb') as f:
  while b:=f.read(1024*1024):h.update(b)
 if p.stat().st_size!=expected['bytes'] or h.hexdigest()!=expected['sha256']:raise ValueError('Shared cache checksum mismatch')
 p.chmod(0o444);s=p.stat();identities[name]=[s.st_dev,s.st_ino,s.st_size,s.st_mtime_ns]
p=root/'.ready.tmp';p.write_text(json.dumps({**v,'identities':identities}));p.chmod(0o444);os.replace(p,root/'.ready.json')
'''
CHECK=r'''
import json,pathlib,sys
root=pathlib.Path('/dev/shm');expected=json.loads(sys.argv[1]);p=root/'.ready.json'
if not p.exists():print('null');raise SystemExit
v=json.loads(p.read_text())
if any(v.get(k)!=expected[k] for k in ('key','files','container_id','started_at')):raise ValueError('Cache identity changed')
for name,identity in v['identities'].items():
 s=(root/name).stat()
 if [s.st_dev,s.st_ino,s.st_size,s.st_mtime_ns]!=identity:raise ValueError('Cache object changed')
print(json.dumps(v))
'''


def ready(host,cache,row):
    labels=row['Config'].get('Labels') or {}
    if labels.get(OWNER)!=cache['key'] or row.get('Image')!=cache['image']:raise ValueError('Cache owner image or identity differs')
    if not row['State'].get('Running'):return False
    expected={k:cache[k] for k in ('key','files')}
    expected.update(container_id=row['Id'],started_at=row['State']['StartedAt'])
    value=json.loads(command(host,['docker','exec',cache['container'],'python3','-c',CHECK,json.dumps(expected)]))
    return bool(value)


def users(host,cache):
    names=command(host,['docker','ps','-a','--filter','volume='+cache['volume'],'--format','{{.Names}}']).splitlines()
    return [n for n in names if n!=cache['container']]


def remove_unused(host,cache):
    if users(host,cache):return False
    owner=inspect(host,'container',cache['container'])
    if owner and owner.get('ExecIDs'):return False  # A bulk reader is a live consumer too.
    if inspect(host,'container',cache['container']):command(host,['docker','rm','-f',cache['container']])
    if inspect(host,'volume',cache['volume']):command(host,['docker','volume','rm',cache['volume']])
    return True


def evict_unused(host,keep=None):
    """Reclaim only named RAM caches with no attached consumer containers."""
    keep={keep} if isinstance(keep,str) else set(keep or ())
    ids=command(host,['docker','ps','-aq','--filter','label='+OWNER]).split()
    if not ids:return 0
    rows=json.loads(command(host,['docker','inspect',*ids]));removed=0
    for row in rows:
        key=(row['Config'].get('Labels') or {}).get(OWNER,'')
        if not re.fullmatch(r'[0-9a-f]{64}',key) or key in keep:continue
        cache={'key':key,'container':'swarm-job-'+key[:32],'volume':'swarm-cache-'+key[:32]}
        if row.get('Name','').lstrip('/')!=cache['container']:continue
        volume=inspect(host,'volume',cache['volume'])
        if not volume:continue
        options=volume.get('Options') or {}
        if volume.get('Driver')!='local' or options.get('type')!='tmpfs' or (volume.get('Labels') or {}).get(OWNER)!=key:continue
        removed+=int(remove_unused(host,cache))
    return removed


@contextlib.contextmanager
def lock(host):
    # Cross-process creation is serialized at the existing scheduling owner.
    folder=Path.home()/'.local/state/swarm-control/submission/cache-locks';folder.mkdir(parents=True,exist_ok=True,mode=0o700)
    key=hashlib.sha256(host['machine'].encode()).hexdigest()
    with (folder/(key+'.lock')).open('a+') as f:
        fcntl.flock(f,fcntl.LOCK_EX);yield


def park_verified_owner(host,cache,row):
    """A sealed immutable RAM holder needs scheduling headroom, not a full core.

    Loading/sealing retains its full CPU allowance. Readers execute in their own
    cgroups and are unaffected by the sleeping owner's CPU limit.
    """
    if row.get('HostConfig',{}).get('NanoCpus')!=100000000:
        command(host,['docker','update','--cpus','0.1',cache['container']])


def ensure(host,image,files,load,metrics=None):
    """Load once; callback streams files into the admitted owner container."""
    key,value=contract(image,files);shm_mb=math.ceil(sum(r['bytes'] for r in files.values())/1024**2)+16
    cache={**value,'key':key,'container':'swarm-job-'+key[:32],'volume':'swarm-cache-'+key[:32],
           'memory_mb':shm_mb+128,'shm_mb':shm_mb}
    with lock(host):
        owner=inspect(host,'container',cache['container']);volume=inspect(host,'volume',cache['volume'])
        if volume:validate_volume(volume,cache)
        if owner and ((owner['Config'].get('Labels') or {}).get(OWNER)!=key or owner.get('Image')!=image):raise ValueError('Shared cache owner differs')
        if owner and volume and ready(host,cache,owner):
            park_verified_owner(host,cache,owner)
            if metrics is not None:metrics.update(cache_hit=True,loaded_bytes=0)
            return cache
        if (owner or volume) and not remove_unused(host,cache):raise CapacityWait('Previous shared cache is still attached to workers')
        client=AgentClient(host);requested=False
        try:
            status=client.call('status')
            if status['machine']!=host['machine']:raise ValueError('Cache node identity changed')
            if not status.get('worker_policy',{}).get('accepting_jobs'):raise CapacityWait('Worker paused before shared cache preparation')
            request={'id':key[:32],'container':cache['container'],'group':'shared-cache','cpus':1,'memory_mb':cache['memory_mb']}
            requested=True
            acquired=client.call('resource.acquire',request)['acquired']
            if not acquired and evict_unused(host,keep=key):acquired=client.call('resource.acquire',request)['acquired']
            if not acquired:raise CapacityWait('Waiting for the shared cache RAM reservation')
            command(host,['docker','volume','create','--driver','local','--label',OWNER+'='+key,'--opt','type=tmpfs','--opt','device=tmpfs',
                          '--opt','o=size='+str(shm_mb)+'m,mode=1777,nosuid,nodev,noexec',cache['volume']])
            # tmpfs payloads disappear on a host reboot. An automatically
            # restarted sleeping owner would reserve RAM for an empty cache.
            # Leave it stopped; the next ensure() verifies/reloads it on demand.
            command(host,['docker','run','-d','--pull=never','--name',cache['container'],'--restart','no','--network','none',
                          '--read-only','--user',host.get('container_user','1000:1000'),'--cap-drop','ALL','--security-opt','no-new-privileges',
                          '--memory',str(cache['memory_mb'])+'m','--memory-swap',str(cache['memory_mb'])+'m','--cpus','1','--pids-limit','32',
                          '--label','org.lifetech.worker=true','--label','org.lifetech.job='+key[:32],'--label','org.lifetech.group=shared-cache',
                          '--label',OWNER+'='+key,'--mount','type=volume,src='+cache['volume']+',dst=/dev/shm,volume-nocopy',
                          image,'python3','-c','import time; time.sleep(10**9)'])
            load(cache['container'])
            owner=inspect(host,'container',cache['container'])
            proof={k:cache[k] for k in ('key','files')};proof.update(container_id=owner['Id'],started_at=owner['State']['StartedAt'])
            command(host,['docker','exec',cache['container'],'python3','-c',SEAL,json.dumps(proof)],timeout=180)
            if not ready(host,cache,owner):raise RuntimeError('Cache did not become verified')
            park_verified_owner(host,cache,owner)
            if metrics is not None:metrics.update(cache_hit=False,loaded_bytes=sum(r['bytes'] for r in files.values()))
            return cache
        except BaseException:
            # Incomplete objects are never published or mistaken for reusable data.
            if inspect(host,'container',cache['container']) or inspect(host,'volume',cache['volume']):remove_unused(host,cache)
            raise
        finally:
            if requested:client.call('resource.release',{'id':key[:32]})
            client.close()


def mount_args(host,cache,target='/dev/shm',primary=True):
    path=Path(target)
    if not path.is_absolute() or '..' in path.parts or str(path)!=target or not re.fullmatch(r'/[A-Za-z0-9_./-]+',target) or target=='/':raise ValueError('Invalid cache mount target')
    key,value=contract(cache['image'],cache['files'])
    if cache.get('key')!=key or cache.get('container')!='swarm-job-'+key[:32] or cache.get('volume')!='swarm-cache-'+key[:32]:raise ValueError('Invalid shared cache binding')
    volume=inspect(host,'volume',cache['volume']);owner=inspect(host,'container',cache['container'])
    if not volume or not owner:raise CapacityWait('Shared cache needs preparation')
    validate_volume(volume,cache)
    if not ready(host,cache,owner):raise CapacityWait('Shared cache is not verified')
    label=DEPENDENCY if primary else DEPENDENCY+'-'+key[:12]
    return ['--mount','type=volume,src='+cache['volume']+',dst='+target+',readonly,volume-nocopy','--label',label+'='+cache['container']]


def ensure_derived(host,image,identity,build,load):
    """Materialize an immutable object once on its local owner, then reuse RAM.

    The lookup is metadata only; Docker names, seals and the existing pool remain
    authoritative. Applications supply derivation and authenticated transport.
    Different identities may prepare concurrently; identical requests coalesce.
    """
    if not host.get('local'):raise ValueError('Derivation must run on its authenticated local input owner')
    encoded=json.dumps({'identity':identity,'image':image},sort_keys=True,separators=(',',':'))
    if len(encoded)>65536:raise ValueError('Object identity exceeds metadata bound')
    key=hashlib.sha256(encoded.encode()).hexdigest()
    folder=Path.home()/'.local/state/swarm-control/submission/shared-objects';folder.mkdir(parents=True,exist_ok=True,mode=0o700)
    with (folder/(key+'.lock')).open('a') as lockfile:
        fcntl.flock(lockfile,fcntl.LOCK_EX)
        metadata=folder/(key+'.json')
        if metadata.exists():
            record=json.loads(metadata.read_text());cache=record['cache']
            if record.get('identity')!=identity or cache.get('image')!=image:raise ValueError('Immutable object lookup changed')
            owner=inspect(host,'container',cache['container'])
            if owner and ready(host,cache,owner):return record
        with tempfile.TemporaryDirectory(prefix='jswarm-object-',dir='/dev/shm') as temp:
            root=Path(temp);build(root)
            files={}
            for p in root.iterdir():
                if not p.is_file() or p.is_symlink():raise ValueError('Derived objects must be flat regular files')
                with p.open('rb') as stream:digest=hashlib.file_digest(stream,'sha256').hexdigest()
                files[p.name]={'bytes':p.stat().st_size,'sha256':digest}
            cache=ensure(host,image,files,lambda owner:load(owner,root,files))
        record={'identity':identity,'cache':cache}
        from scripts.swarm_runtime import atomic_json
        atomic_json(metadata,record)
        return record
