"""One Docker-owned, verified RAM volume per immutable object set and node.

The existing resource pool charges its owner once. Consumers mount the volume
read-only and reserve only private memory. No host IPC or payload disk fallback.
"""
import contextlib
from collections import Counter, OrderedDict, defaultdict
import hashlib
import json
import math
import os
import sqlite3
from pathlib import Path
import re
import subprocess
import tempfile
import threading

from scripts.swarm_runtime import AgentClient,CapacityWait,shell_command

OWNER='org.lifetech.cache-owner'
DEPENDENCY='org.lifetech.shared-cache'



def is_managed(cache):
    return cache.get('layout') == 'node-cache-v1'


def validate_binding(cache):
    if is_managed(cache):
        from scripts.launcher_cache import validate
        return validate(cache)
    key, _ = contract(cache['image'], cache['files'])
    if cache.get('key') != key or cache.get('container') != 'swarm-job-'+key[:32] or cache.get('volume') != 'swarm-cache-'+key[:32]:
        raise ValueError('Invalid shared cache binding')
    return cache


def object_path(cache):
    if is_managed(cache):
        validate_binding(cache)
        return cache['path']
    return '/dev/shm'


class pin:
    """Bridge prepare-to-mount and protect transfers; Docker protects live mounts.

    Pins expire after a dead controller exits. Running or stopped Docker
    consumers remain protected independently of pin renewal or agent restart.
    """
    def __init__(self, host, cache, *, token=None, acquire=True):
        import secrets
        self.host, self.cache = host, cache
        self.token = token or secrets.token_hex(32)
        self.acquire = acquire
        self.stop = threading.Event()
        self.client = None
        self.error = None

    def __enter__(self):
        if not is_managed(self.cache):return self
        validate_binding(self.cache)
        self.client = AgentClient(self.host)
        try:
            if self.acquire:
                self.client.call('resource.cache.pin', {'key':self.cache['key'], 'token':self.token, 'machine':self.host['machine']}, timeout=120)
            self.thread = threading.Thread(target=self.renew, daemon=True, name='cache-pin')
            self.thread.start()
        except BaseException as error:
            self.client.close()
            raise CapacityWait('Cache miss; prepare inputs again') from error
        return self

    def renew(self):
        while not self.stop.wait(30):
            try:self.client.call('resource.cache.renew', {'key':self.cache['key'], 'token':self.token, 'machine':self.host['machine']}, timeout=20)
            except Exception as error:
                self.error = error
                return

    def __exit__(self, *exc):
        if self.client is None:return
        self.stop.set()
        self.thread.join(timeout=25)
        try:self.client.call('resource.cache.release', {'key':self.cache['key'], 'token':self.token, 'machine':self.host['machine']}, timeout=20)
        except Exception:pass  # Expiry and actual Docker mounts remain authoritative.
        finally:self.client.close()


def _ensure_managed(host, image, files, load, metrics=None, on_bound=None):
    import secrets
    key, value = contract(image, files)
    size = math.ceil(sum(r['bytes'] for r in value['files'].values()) / 1024**2) + 16
    cache = {**value, 'host':host['host'], 'key':key, 'layout':'node-cache-v1',
             'container':'swarm-node-cache', 'volume':'swarm-node-cache',
             'path':'/cache/objects/'+key, 'subpath':'objects/'+key, 'memory_mb':size, 'shm_mb':size}
    token = secrets.token_hex(32)
    client = AgentClient(host)
    try:
        result = client.call('resource.cache.begin', {'cache':cache, 'token':token, 'machine':host['machine']}, timeout=120)
        if result['state'] in ('waiting', 'capacity'):
            raise CapacityWait('Shared cache awaits loading or RAM headroom')
        if on_bound:on_bound(cache)
        if result['state'] == 'ready':
            client.call('resource.cache.check', {'key':key, 'machine':host['machine']}, timeout=120)
            if metrics is not None:metrics.update(cache_hit=True, loaded_bytes=0)
            return cache
        with pin(host, cache, token=token, acquire=False):
            load(cache['container'], cache['path'])
            client.call('resource.cache.seal', {'key':key, 'token':token, 'machine':host['machine']}, timeout=120)
        if metrics is not None:metrics.update(cache_hit=False, loaded_bytes=sum(r['bytes'] for r in files.values()))
        return cache
    except CapacityWait:raise
    except (RuntimeError, ValueError) as error:
        raise CapacityWait('Shared cache preparation must retry: '+str(error)[:200]) from error
    finally:client.close()


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
root=pathlib.Path(sys.argv[2] if len(sys.argv)>2 else '/dev/shm');expected=json.loads(sys.argv[1]);p=root/'.ready.json'
if not p.exists():print('null');raise SystemExit
v=json.loads(p.read_text())
if any(v.get(k)!=expected[k] for k in ('key','files','container_id','started_at')):raise ValueError('Cache identity changed')
for name,identity in v['identities'].items():
 s=(root/name).stat()
 if [s.st_dev,s.st_ino,s.st_size,s.st_mtime_ns]!=identity:raise ValueError('Cache object changed')
print(json.dumps(v))
'''


def ready(host,cache,row):
    if is_managed(cache):
        validate_binding(cache)
        client=AgentClient(host)
        try:
            client.call('resource.cache.check', {'key':cache['key'], 'machine':host['machine']}, timeout=120)
            return True
        except (RuntimeError,ValueError):return False
        finally:client.close()
    labels=row['Config'].get('Labels') or {}
    if labels.get(OWNER)!=cache['key'] or row.get('Image')!=cache['image']:raise ValueError('Cache owner image or identity differs')
    if not row['State'].get('Running'):return False
    expected={k:cache[k] for k in ('key','files')}
    expected.update(container_id=row['Id'],started_at=row['State']['StartedAt'])
    value=json.loads(command(host,['docker','exec',cache['container'],'python3','-c',CHECK,json.dumps(expected)]))
    return bool(value)


def users(host,cache):
    if is_managed(cache):
        # Used for diagnostics only; pin/eviction decisions belong to the node.
        return command(host,['docker','ps','-a','--filter','volume='+cache['volume'],'--format','{{.Names}}']).splitlines()
    names=command(host,['docker','ps','-a','--filter','volume='+cache['volume'],'--format','{{.Names}}']).splitlines()
    return [n for n in names if n!=cache['container']]


def _remove_unreferenced(host,cache,*,expected_owner=None):
    if users(host,cache):return False
    owner=inspect(host,'container',cache['container'])
    if expected_owner and (not owner or owner['Id']!=expected_owner):return False
    if owner and owner.get('ExecIDs'):return False  # A bulk reader is a live consumer too.
    if owner:
        try:command(host,['docker','rm','-f',owner['Id']])
        except RuntimeError as error:
            if not any(t in str(error).lower() for t in ('no such','not found','removal of container')):raise
    if inspect(host,'volume',cache['volume']):
        try:command(host,['docker','volume','rm',cache['volume']])
        except RuntimeError as error:
            # A worker can attach after users() and before Docker removes the
            # volume. Docker's reference check wins; retry after that consumer.
            if 'volume is in use' in str(error).lower() or 'volume is being used' in str(error).lower():return False
            raise
    return True


class _QueueOwnership:
    """Disposable read model of the existing queue; never a claims ledger.

    SQLite data_version detects commits by other connections, including WAL
    writes. Unchanged rows reuse parsed bindings by content digest. No read
    transaction survives a call and no timed/stale eligibility cache is used.
    """
    def __init__(self,path,identity):
        self.identity=identity
        self.db=sqlite3.connect(path.as_uri()+'?mode=ro',uri=True,timeout=2,check_same_thread=False)
        self.version=None
        self.rows={}
        self.eligible={}
        self.uses={}

    @staticmethod
    def parse(request,node):
        value=json.loads(request or '{}')
        if not isinstance(value,dict):raise ValueError('Invalid cache ownership request')
        bindings=defaultdict(set)
        def visit(item,owner):
            if isinstance(item,dict):
                owner=item.get('host',owner)
                key=item.get('key')
                if (isinstance(key,str) and re.fullmatch(r'[a-f0-9]{64}',key)
                        and item.get('container')=='swarm-job-'+key[:32]
                        and item.get('volume')=='swarm-cache-'+key[:32]
                        and isinstance(owner,str) and owner):
                    bindings[owner.split('#',1)[0]].add(key)
                for child in item.values():visit(child,owner)
            elif isinstance(item,list):
                for child in item:visit(child,owner)
        visit(value,node)
        legacy=(value.get('inputs_ready') is False or not request) and value.get('cache_claims_version')!=1
        owner=value.get('worker_host') or node
        if owner is not None and not isinstance(owner,str):raise ValueError('Invalid preparation host')
        return bindings,legacy,owner.split('#',1)[0] if owner else None

    def refresh(self):
        version=self.db.execute('PRAGMA data_version').fetchone()[0]
        if self.version==version:return
        terminal=defaultdict(set);protected=defaultdict(set);uses=defaultdict(Counter)
        blocked=set();blocked_all=False;rows={}
        self.db.execute('BEGIN')
        try:
            for ident,state,node,request in self.db.execute('SELECT rowid,state,host,request FROM submissions'):
                if request is not None and not isinstance(request,str):raise ValueError('Invalid queue request encoding')
                digest=hashlib.sha256((request or '').encode()).digest()
                previous=self.rows.get(ident)
                summary=previous[2] if previous and previous[:2]==(node,digest) else self.parse(request,node)
                rows[ident]=(node,digest,summary)
                bindings,legacy,owner=summary
                finished=state in ('completed','failed','cancelled')
                for host,keys in bindings.items():
                    (terminal if finished else protected)[host].update(keys)
                    uses[host].update(keys)
                if not finished and state in ('accepted','preparing') and legacy:
                    if not owner:blocked_all=True
                    else:blocked.add(owner)
                    blocked.update(bindings)
        finally:
            self.db.rollback()
        self.eligible={} if blocked_all else {host:keys-protected[host] for host,keys in terminal.items() if host not in blocked}
        self.uses=uses
        self.rows=rows
        # A commit during our snapshot forces the next call to refresh again.
        self.version=version if self.db.execute('PRAGMA data_version').fetchone()[0]==version else None


_OWNERSHIP_LOCK=threading.RLock()
_OWNERSHIP_QUEUES=OrderedDict()


def _ownership_for(host,path=None):
    path=Path(path or os.getenv('SWARM_SUBMISSION_QUEUE',str(Path.home()/'.local/state/swarm-control/submission/queue.sqlite'))).resolve()
    physical=host['host'].split('#',1)[0]
    with _OWNERSHIP_LOCK:
        try:
            stat=path.stat()
            identity=(os.getpid(),stat.st_dev,stat.st_ino,stat.st_mtime_ns,stat.st_size)
            entry=_OWNERSHIP_QUEUES.get(path)
            if entry and entry.identity!=identity:
                entry.db.close();del _OWNERSHIP_QUEUES[path];entry=None
            if entry is None:
                entry=_QueueOwnership(path,identity)
                _OWNERSHIP_QUEUES[path]=entry
                while len(_OWNERSHIP_QUEUES)>4:
                    _,old=_OWNERSHIP_QUEUES.popitem(last=False);old.db.close()
            _OWNERSHIP_QUEUES.move_to_end(path)
            entry.refresh()
            return set(entry.eligible.get(physical,())),dict(entry.uses.get(physical,{}))
        except (OSError,ValueError,TypeError,sqlite3.Error,RecursionError):
            entry=_OWNERSHIP_QUEUES.pop(path,None)
            if entry:entry.db.close()
            return set(),{}


def finished_cache_keys(host,path=None):
    """Terminal-only bindings across every namespace, with fresh commit checks.

    Preparing, queued, retrying and unknown-state consumers remain protected.
    Legacy incomplete preparation blocks its physical node (or every node when
    unassigned). Missing/corrupt/locked queues fail closed. Returned sets are
    copies; callers cannot change the cached read model.
    """
    return _ownership_for(host,path)[0]


def remove_unused(host,cache,*,terminal=None):
    """Delete terminal job data only when the existing queue permits it."""
    if is_managed(cache):
        # Completion releases the runtime/transfer pin. Retain reusable bytes
        # within the node budget; pressure or explicit cleanup evicts them.
        return False
    if terminal not in ('completed','failed','cancelled'):return False
    if cache['key'] not in finished_cache_keys(host):return False
    return _remove_unreferenced(host,cache)


def _reclaim_completed(host,keep=None,*,keys=None,required_mb=None):
    if required_mb is not None and (type(required_mb) is not int or required_mb<1):raise ValueError('Positive reclamation budget required')
    candidates,uses=_ownership_for(host)
    if keys is not None:candidates.intersection_update(keys)
    keep={keep} if isinstance(keep,str) else set(keep or ())
    candidates-=keep
    if not candidates:return 0
    client=AgentClient(host) if required_mb is not None else None
    def enough():
        if client is None:return False
        available=client.call('resource.capacity').get('available_mb')
        if not isinstance(available,(int,float)) or isinstance(available,bool) or not math.isfinite(available) or available<0:
            raise ValueError('Invalid admission headroom during cache reclamation')
        return available>=required_mb
    try:
        if enough():return 0
        ids=command(host,['docker','ps','-aq','--filter','label='+OWNER]).split()
        if not ids:return 0
        rows=json.loads(command(host,['docker','inspect',*ids]));removed=0
        def order(row):
            key=(row['Config'].get('Labels') or {}).get(OWNER,'')
            return (uses.get(key,0) if client else 0,row.get('Created',''),key)
        for row in sorted(rows,key=order):
            key=(row['Config'].get('Labels') or {}).get(OWNER,'')
            if key not in candidates:continue
            cache={'key':key,'container':'swarm-job-'+key[:32],'volume':'swarm-cache-'+key[:32]}
            if row.get('Name','').lstrip('/')!=cache['container']:continue
            volume=inspect(host,'volume',cache['volume'])
            if not volume:continue
            options=volume.get('Options') or {}
            if volume.get('Driver')!='local' or options.get('type')!='tmpfs' or (volume.get('Labels') or {}).get(OWNER)!=key:continue
            # A new preparation may have published bindings during Docker probes.
            if key not in finished_cache_keys(host):continue
            if _remove_unreferenced(host,cache):
                removed+=1
                # Observe actual headroom, never count configured limits as RAM
                # freed. Background sweeps intentionally clear every candidate.
                if enough():break
        return removed
    finally:
        if client:client.close()


def evict_unused(host,keep=None,*,keys=None,required_mb=None):
    """Compatibility: even explicit candidates need durable terminal job proof."""
    if required_mb is not None and (type(required_mb) is not int or required_mb<1):raise ValueError('Positive reclamation budget required')
    if keys is None:return 0
    return _reclaim_completed(host,keep,keys=keys,required_mb=required_mb)


def reclaim_completed(host,keep=None,*,required_mb=None):
    # Legacy terminal-only owners remain compatible during a rolling migration.
    removed = _reclaim_completed(host,keep,required_mb=required_mb)
    if required_mb is None or 'node-cache-v1' not in host.get('capabilities',[]):return removed
    client = AgentClient(host)
    try:
        args = {}
        if required_mb is not None:
            available = client.call('resource.capacity')['available_mb']
            if available >= required_mb:return removed
            args['required_mb'] = max(1, math.ceil(required_mb-available))
        if keep:args['keep']=sorted({keep} if isinstance(keep,str) else set(keep))
        # Runtime pins and explicit keep keys protect the current inputs.
        return removed + len(client.call('resource.cache.prune', {**args,'machine':host['machine']}, timeout=120)['removed'])
    finally:client.close()


def park_verified_owner(host,cache,row):
    """A sealed immutable RAM holder needs scheduling headroom, not a full core.

    Loading/sealing retains its full CPU allowance. Readers execute in their own
    cgroups and are unaffected by the sleeping owner's CPU limit.
    """
    if row.get('HostConfig',{}).get('NanoCpus')!=100000000:
        command(host,['docker','update','--cpus','0.1',cache['container']])


def ensure(host,image,files,load,metrics=None,*,on_bound=None,load_path=False):
    """Load once; callback streams files into the admitted owner container."""
    if load_path:
        client=AgentClient(host)
        try:
            host['capabilities']=client.call('status').get('capabilities',[])
            managed='node-cache-v1' in host['capabilities']
        finally:client.close()
        if managed:return _ensure_managed(host,image,files,load,metrics,on_bound)
    key,value=contract(image,files);shm_mb=math.ceil(sum(r['bytes'] for r in files.values())/1024**2)+16
    cache={**value,'host':host['host'],'key':key,'container':'swarm-job-'+key[:32],'volume':'swarm-cache-'+key[:32],
           'memory_mb':shm_mb+128,'shm_mb':shm_mb}
    # Publish queue ownership before inspecting/reusing an existing cache.
    if on_bound:on_bound(cache)
    owner=inspect(host,'container',cache['container']);volume=inspect(host,'volume',cache['volume'])
    if volume:validate_volume(volume,cache)
    if owner and ((owner['Config'].get('Labels') or {}).get(OWNER)!=key or owner.get('Image')!=image):raise ValueError('Shared cache owner differs')
    if owner and volume and ready(host,cache,owner):
        park_verified_owner(host,cache,owner)
        if metrics is not None:metrics.update(cache_hit=True,loaded_bytes=0)
        return cache
    if owner and owner.get('State',{}).get('Status') not in ('exited','dead'):
        raise CapacityWait('Shared cache is being prepared')
    if owner and not _remove_unreferenced(host,cache,expected_owner=owner['Id']):raise CapacityWait('Previous shared cache is still attached to workers')
    if not owner and volume and users(host,cache):raise CapacityWait('Previous shared cache is still attached to workers')
    client=AgentClient(host);created=None;published=False
    try:
        status=client.call('status')
        if status['machine']!=host['machine']:raise ValueError('Cache node identity changed')
        if not status.get('worker_policy',{}).get('accepting_jobs'):raise CapacityWait('Worker paused before shared cache preparation')
        request={'id':key[:32],'container':cache['container'],'group':'shared-cache','cpus':1,'memory_mb':cache['memory_mb']}
        acquired=client.call('resource.acquire',request)['acquired']
        if not acquired:
            _reclaim_completed(host,keep=key,required_mb=cache['memory_mb'])
            acquired=client.call('resource.acquire',request)['acquired']
        if not acquired:raise CapacityWait('Waiting for the shared cache RAM reservation')
        command(host,['docker','volume','create','--driver','local','--label',OWNER+'='+key,'--opt','type=tmpfs','--opt','device=tmpfs',
                      '--opt','o=size='+str(shm_mb)+'m,mode=1777,nosuid,nodev,noexec',cache['volume']])
        # tmpfs payloads disappear on a host reboot. An automatically
        # restarted sleeping owner would reserve RAM for an empty cache.
        # Leave it stopped; the next ensure() verifies/reloads it on demand.
        created=command(host,['docker','run','-d','--pull=never','--name',cache['container'],'--restart','no','--network','none','--log-driver','none',
                      '--read-only','--user',host.get('container_user','1000:1000'),'--cap-drop','ALL','--security-opt','no-new-privileges',
                      '--memory',str(cache['memory_mb'])+'m','--memory-swap',str(cache['memory_mb'])+'m','--cpus','1','--pids-limit','32',
                      '--label','org.lifetech.worker=true','--label','org.lifetech.job='+key[:32],'--label','org.lifetech.group=shared-cache',
                      '--label',OWNER+'='+key,'--mount','type=volume,src='+cache['volume']+',dst=/dev/shm,volume-nocopy',
                      image,'python3','-c','import time; time.sleep(10**9)']).strip()
        load(cache['container'])
        owner=inspect(host,'container',cache['container'])
        proof={k:cache[k] for k in ('key','files')};proof.update(container_id=owner['Id'],started_at=owner['State']['StartedAt'])
        command(host,['docker','exec',cache['container'],'python3','-c',SEAL,json.dumps(proof)],timeout=180)
        if not ready(host,cache,owner):raise RuntimeError('Cache did not become verified')
        published=True
        park_verified_owner(host,cache,owner)
        if metrics is not None:metrics.update(cache_hit=False,loaded_bytes=sum(r['bytes'] for r in files.values()))
        return cache
    except BaseException as error:
        owner=inspect(host,'container',cache['container'])
        if created and not published and owner and owner['Id']==created:
            # A losing creator never removes the winning loader or releases
            # its admission. Only our own unpublished attempt is disposable.
            _remove_unreferenced(host,cache,expected_owner=created)
        elif isinstance(error,RuntimeError) and (owner or 'already in use' in str(error).lower()):
            raise CapacityWait('Shared cache preparation changed; retry') from error
        raise
    finally:
        if created:client.call('resource.release',{'id':key[:32]})
        client.close()


def mount_args(host,cache,target='/dev/shm',primary=True):
    if is_managed(cache):
        validate_binding(cache)
        if not ready(host,cache,None):raise CapacityWait('Cache miss; prepare inputs again')
        path=Path(target)
        if not path.is_absolute() or '..' in path.parts or str(path)!=target or not re.fullmatch(r'/[A-Za-z0-9_./-]+',target) or target=='/':raise ValueError('Invalid cache mount target')
        return ['--mount','type=volume,src='+cache['volume']+',dst='+target+',readonly,volume-nocopy,volume-subpath='+cache['subpath'],
                '--label',DEPENDENCY+'-'+cache['key'][:12]+'='+cache['key']]
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


def ensure_derived(host,image,identity,build,load,*,on_bound=None,load_path=False):
    """Materialize an immutable object once on its local owner, then reuse RAM.

    The lookup is metadata only; Docker names, seals and the existing pool remain
    authoritative. Applications supply derivation and authenticated transport.
    Builders use private RAM directories; Docker elects the shared object loader.
    """
    if not host.get('local'):raise ValueError('Derivation must run on its authenticated local input owner')
    encoded=json.dumps({'identity':identity,'image':image},sort_keys=True,separators=(',',':'))
    if len(encoded)>65536:raise ValueError('Object identity exceeds metadata bound')
    key=hashlib.sha256(encoded.encode()).hexdigest()
    folder=Path.home()/'.local/state/swarm-control/submission/shared-objects';folder.mkdir(parents=True,exist_ok=True,mode=0o700)
    metadata=folder/(key+'.json')
    if metadata.exists():
        record=json.loads(metadata.read_text());cache=record['cache']
        if record.get('identity')!=identity or cache.get('image')!=image:raise ValueError('Immutable object lookup changed')
        if on_bound:on_bound({**cache,'host':host['host']})
        migrate=False
        if load_path and not is_managed(cache):
            client=AgentClient(host)
            try:host['capabilities']=client.call('status').get('capabilities',[])
            finally:client.close()
            migrate='node-cache-v1' in host['capabilities']
        if not migrate:
            owner=inspect(host,'container',cache['container'])
            if owner and ready(host,cache,owner):return record
    with tempfile.TemporaryDirectory(prefix='jswarm-object-',dir='/dev/shm') as temp:
        root=Path(temp);build(root)
        files={}
        for p in root.iterdir():
            if not p.is_file() or p.is_symlink():raise ValueError('Derived objects must be flat regular files')
            with p.open('rb') as stream:digest=hashlib.file_digest(stream,'sha256').hexdigest()
            files[p.name]={'bytes':p.stat().st_size,'sha256':digest}
        if load_path:
            cache=ensure(host,image,files,lambda owner,path='/dev/shm':load(owner,root,files,path),on_bound=on_bound,load_path=True)
        else:
            cache=ensure(host,image,files,lambda owner:load(owner,root,files),on_bound=on_bound)
    record={'identity':identity,'cache':cache}
    from scripts.swarm_runtime import atomic_json
    atomic_json(metadata,record)
    return record


def capacity_for_work(host,client,group,memory_mb):
    """Reclaim before placement too, so idle nodes cannot starve behind caches."""
    capacity=client.call('resource.capacity',{'group':group})
    if capacity['available_mb']<memory_mb:
        reclaim_completed(host,required_mb=memory_mb)
        capacity=client.call('resource.capacity',{'group':group})
    return capacity
