"""One bounded RAM cache per enrolled node; objects are pinned only while in use.

The keeper survives agent replacement. Its RAM manifest and expiring pins survive
with it; a reboot loses both. No queue or job state authorizes object eviction.
"""
import hashlib
import json
import math
import os
import re
import secrets
import subprocess
import threading
import time

NAME = 'swarm-node-cache'
LABEL = 'org.lifetech.node-cache'
LAYOUT = 'node-cache-v1'
MIB = 1024 ** 2

# Executed inside the keeper, so metadata and payloads share the same RAM-only
# lifetime. flock also serializes a successor agent with in-flight old requests.
OBJECT_RUNTIME = r'''
import fcntl,hashlib,json,os,pathlib,re,shutil,stat,sys,time
p=json.loads(sys.argv[1]);root=pathlib.Path('/cache');root.mkdir(exist_ok=True)
with (root/'.lock').open('a+') as lock:
 fcntl.flock(lock,fcntl.LOCK_EX)
 started=time.monotonic();state=root/'.state.json';now=time.time()
 entries=json.loads(state.read_text()) if state.exists() else {}
 telemetry=root/'.metrics.json'
 metrics=json.loads(telemetry.read_text()) if telemetry.exists() else {'since':now,'counters':{},'operations':{},'transfers':{}}
 def count(name,value=1):metrics['counters'][name]=metrics['counters'].get(name,0)+value
 for item in entries.values():item['pins']={k:v for k,v in item['pins'].items() if v>now}
 def save():
  for path,value in ((state,entries),(telemetry,metrics)):
   tmp=path.with_suffix('.tmp');tmp.write_text(json.dumps(value));os.replace(tmp,path)
 def used():return sum(i['cache']['shm_mb'] for i in entries.values())
 def protected(key,item):return bool(item['pins']) or key in p.get('consumers',[]) or key in p.get('keep',[]) or p.get('busy',False)
 def drop(key,reason='explicit'):
  item=entries.get(key)
  if item is None:return {'removed':False,'reason':'absent'}
  if protected(key,item):return {'removed':False,'reason':'pinned'}
  path=root/'objects'/key
  if path.is_symlink():raise ValueError('Cache directory changed')
  if path.exists():
   path.chmod(0o700);shutil.rmtree(path)
  del entries[key]
  count('evictions');count('evicted_allocated_mb',item['cache']['shm_mb']);count('evictions_'+reason)
  return {'removed':True,'key':key}
 op=p['op'];key=p.get('key');result={}
 try:
  if key is not None and not re.fullmatch('[a-f0-9]{64}',key):raise ValueError('Invalid object key')
  # A dead loader has no future value. Live Docker mounts/execs and renewable
  # pins remain authoritative even when a controller or agent is replaced.
  for k,item in list(entries.items()):
   if item['phase']=='loading' and not protected(k,item):drop(k,'expired_load')
  if op=='inventory':
   result={'items':[dict(i['cache'],phase=i['phase'],pinned=protected(k,i),last_used=i['last_used']) for k,i in entries.items()], 'used_mb':used()}
  elif op=='prune':
   removed=[]
   for k,i in sorted(list(entries.items()),key=lambda kv:kv[1]['last_used']):
    if p.get('required_mb') is not None and sum(r['shm_mb'] for r in removed)>=p['required_mb']:break
    r=drop(k,'pressure' if p.get('required_mb') is not None else 'explicit_prune')
    if r['removed']:removed.append(i['cache'])
   result={'removed':removed,'used_mb':used()}
  elif op=='drop':result=drop(key)
  elif op=='transfer':
   item=entries.get(key)
   if item is None:raise ValueError('Transferred cache object disappeared')
   if p['bytes']!=sum(f['bytes'] for f in item['cache']['files'].values()):raise ValueError('Transfer byte count differs')
   token=p['transfer_id']+':'+p['direction']
   if token not in metrics['transfers']:
    count('peer_transfers_'+p['direction']);count('peer_bytes_'+p['direction'],p['bytes'])
    metrics['transfers'][token]=now
    while len(metrics['transfers'])>2048:del metrics['transfers'][next(iter(metrics['transfers']))]
   result={'recorded':True}
  elif op=='begin':
   item=entries.get(key)
   if item and item['phase']=='ready':
    count('begin_hits');count('reuse_bytes',sum(f['bytes'] for f in item['cache']['files'].values()))
    item['last_used']=now;result={'state':'ready','cache':item['cache']}
   else:
    count('begin_misses')
    if item and protected(key,item):count('load_waits');result={'state':'waiting'}
    else:
     if item:drop(key,'replaced_load')
     cache=p['cache']
     if used()+cache['shm_mb']>p['budget_mb']:count('capacity_waits');result={'state':'capacity'}
     else:
      path=root/'objects'/key;path.mkdir(parents=True,mode=0o700)
      entries[key]={'cache':cache,'phase':'loading','last_used':now,'pins':{p['token']:now+120}}
      count('load_starts');result={'state':'loading','cache':cache}
  elif op in ('pin','renew','release','seal','check'):
   item=entries.get(key)
   if item is None:
    if op=='check':count('check_misses')
    raise ValueError('Cache miss; prepare object again')
   path=root/'objects'/key
   if op=='pin':
    if item['phase']!='ready':raise ValueError('Cache object is not ready')
    item['pins'][p['token']]=now+120;item['last_used']=now
   elif op=='renew':
    if p['token'] not in item['pins']:raise ValueError('Cache pin expired')
    item['pins'][p['token']]=now+120
   elif op=='release':item['pins'].pop(p['token'],None)
   elif op=='seal':
    if item['phase']!='loading' or p['token'] not in item['pins']:raise ValueError('Cache loader expired')
    files=item['cache']['files'];identities={}
    if set(x.name for x in path.iterdir())!=set(files):raise ValueError('Unexpected cache files')
    for name,expected in files.items():
     f=path/name;s=f.lstat()
     if not stat.S_ISREG(s.st_mode) or s.st_size!=expected['bytes']:raise ValueError('Cache file identity differs')
     h=hashlib.sha256()
     with f.open('rb') as stream:
      while block:=stream.read(1024*1024):h.update(block)
     if h.hexdigest()!=expected['sha256']:raise ValueError('Cache checksum differs')
     f.chmod(0o444);s=f.stat();identities[name]=[s.st_dev,s.st_ino,s.st_size,s.st_mtime_ns]
    proof={**item['cache'],'container_id':p['owner_id'],'started_at':p['started_at'],'identities':identities}
    (path/'.ready.json').write_text(json.dumps(proof));(path/'.ready.json').chmod(0o444)
    path.chmod(0o555)
    item['phase']='ready';item['last_used']=now;item['pins'].pop(p['token'],None)
    count('loads_sealed');count('bytes_loaded',sum(f['bytes'] for f in files.values()))
   else:
    if item['phase']!='ready':count('check_misses');raise ValueError('Cache miss; prepare object again')
    proof=json.loads((path/'.ready.json').read_text())
    if proof['container_id']!=p['owner_id'] or proof['started_at']!=p['started_at']:raise ValueError('Cache keeper changed')
    for name,identity in proof['identities'].items():
     f=path/name;s=f.lstat()
     if not stat.S_ISREG(s.st_mode) or s.st_mode&0o222 or [s.st_dev,s.st_ino,s.st_size,s.st_mtime_ns]!=identity:raise ValueError('Cache file changed')
    count('check_hits');item['last_used']=now
   result={'cache':item['cache'],'used_mb':used()}
  else:raise ValueError('Unknown cache operation')
 except BaseException:
  count('errors');raise
 finally:
  elapsed=time.monotonic()-started
  operation=metrics['operations'].setdefault(op,{'calls':0,'total_seconds':0,'max_seconds':0})
  operation['calls']+=1;operation['total_seconds']+=elapsed;operation['max_seconds']=max(operation['max_seconds'],elapsed)
  save()
 result['summary']={'objects':len(entries),'pinned_objects':sum(protected(k,i) for k,i in entries.items()),'loading_objects':sum(i['phase']=='loading' for i in entries.values()),'used_mb':used()}
 if op=='inventory':result['metrics']={k:v for k,v in metrics.items() if k!='transfers'}
 print(json.dumps(result))
'''


def validate(cache):
    if not isinstance(cache, dict) or cache.get('layout') != LAYOUT:
        raise ValueError('Expected a managed cache binding')
    image, files = cache.get('image'), cache.get('files')
    if not isinstance(image, str) or not re.fullmatch(r'sha256:[a-f0-9]{64}', image):
        raise ValueError('Pinned cache image required')
    if not isinstance(files, dict) or not 1 <= len(files) <= 128:
        raise ValueError('Invalid cache files')
    for name, row in files.items():
        if (not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.-]{0,119}', name)
                or not isinstance(row, dict) or set(row) != {'bytes', 'sha256'}
                or type(row['bytes']) is not int or row['bytes'] < 1
                or not isinstance(row['sha256'], str) or not re.fullmatch(r'[a-f0-9]{64}', row['sha256'])):
            raise ValueError('Invalid immutable cache file')
    key = hashlib.sha256(json.dumps({'image': image, 'files': files}, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    if (cache.get('key') != key or cache.get('container') != NAME or cache.get('volume') != NAME
            or cache.get('subpath') != 'objects/' + key or cache.get('path') != '/cache/objects/' + key):
        raise ValueError('Managed cache identity differs')
    required = math.ceil(sum(r['bytes'] for r in files.values()) / MIB) + 16
    if type(cache.get('shm_mb')) is not int or cache['shm_mb'] != required or cache.get('memory_mb') != required:
        raise ValueError('Managed cache budget differs')
    return cache


class CacheManager:
    def __init__(self, *, capacity, machine, accepting=lambda: True, run=subprocess.run, name=NAME, memory_mb=None):
        self.capacity, self.machine, self.accepting, self.run = capacity, machine, accepting, run
        # A custom name is for isolated Docker integration tests only.
        self.name = name
        self.lock = threading.RLock()
        self.summary = {}
        self.memory_mb = memory_mb
        self.control_metrics = {'since':time.time(), 'operations':{}}
        self.maintenance_stop = threading.Event()

    def start_maintenance(self):
        if getattr(self, 'maintenance_thread', None):return
        def maintain():
            while not self.maintenance_stop.wait(30):
                # Never queue maintenance behind a caller that is loading data.
                if not self.lock.acquire(blocking=False):continue
                try:self.dispatch('inventory', {})
                except Exception:pass  # Next inventory retries; admission stays strict.
                finally:self.lock.release()
        self.maintenance_thread = threading.Thread(target=maintain,daemon=True,name='cache-maintenance')
        self.maintenance_thread.start()

    def docker(self, args, *, missing=False):
        result = self.run(['docker', *args], capture_output=True, text=True, timeout=120)
        if result.returncode:
            if missing and any(s in result.stderr.lower() for s in ('no such', 'not found')):
                return None
            raise RuntimeError('Node cache Docker operation failed: ' + result.stderr[-300:])
        return result.stdout

    def owner(self):
        raw = self.docker(['container', 'inspect', self.name], missing=True)
        if raw is None:
            return None
        row = json.loads(raw)[0]
        labels = row['Config'].get('Labels') or {}
        if labels.get(LABEL) != self.machine or row['Name'].lstrip('/') != self.name:
            raise ValueError('Node cache keeper identity differs')
        mounts = [m for m in row['Mounts'] if m.get('Name') == self.name and m['Destination'] == '/cache']
        volume = json.loads(self.docker(['volume', 'inspect', self.name]))[0]
        options = volume.get('Options') or {}
        if (len(mounts) != 1 or volume.get('Driver') != 'local' or options.get('type') != 'tmpfs'
                or options.get('device') != 'tmpfs' or (volume.get('Labels') or {}).get(LABEL) != self.machine):
            raise ValueError('Node cache is not the owned RAM volume')
        return row

    def budget(self):
        # A read-only inventory must not acquire the global admission lock or
        # inspect every Docker worker merely to learn total node RAM.
        total = int(self.memory_mb() if self.memory_mb else self.capacity()['memory_mb'])
        configured = os.getenv('SWARM_CACHE_BUDGET_MB')
        budget = int(configured) if configured else max(64, total // 4)
        if not 32 <= budget <= total - 128:
            raise ValueError('Cache budget must leave node memory headroom')
        return budget

    def ensure_owner(self, image):
        row = self.owner()
        if row is not None:
            if not row['State']['Running']:
                # Never restart a holder with surviving readers: its old RAM
                # may still be mounted in those containers.
                refs = self.docker(['ps', '-aq', '--no-trunc', '--filter', 'volume=' + self.name]).split()
                if any(ident != row['Id'] for ident in refs):
                    raise ValueError('Stopped cache keeper still has readers')
                self.docker(['rm', row['Id']])
            else:
                return row
        budget = self.budget()
        if self.capacity()['available_mb'] < 128:
            raise ValueError('Cache keeper awaits node RAM headroom')
        existing = self.docker(['volume', 'inspect', self.name], missing=True)
        if existing:
            v = json.loads(existing)[0]
            if (v.get('Labels') or {}).get(LABEL) != self.machine:
                raise ValueError('Foreign node cache volume')
            # Recreate only after Docker confirms there are no surviving readers.
            self.docker(['volume', 'rm', self.name])
        self.docker(['volume', 'create', '--driver', 'local', '--label', LABEL + '=' + self.machine,
                     '--opt', 'type=tmpfs', '--opt', 'device=tmpfs', '--opt',
                     'o=size=' + str(budget) + 'm,mode=0700,uid=1000,gid=1000,nosuid,nodev,noexec', self.name])
        self.docker(['run', '-d', '--pull=never', '--name', self.name, '--restart', 'no', '--network', 'none',
                     '--log-driver', 'none', '--read-only', '--user', '1000:1000', '--cap-drop', 'ALL',
                     '--security-opt', 'no-new-privileges', '--memory', '128m', '--memory-swap', '128m',
                     '--cpus', '1', '--pids-limit', '128', '--label', LABEL + '=' + self.machine,
                     '--label', 'org.lifetech.worker=true', '--label', 'org.lifetech.group=shared-cache',
                     '--label', 'org.lifetech.job=' + hashlib.sha256(self.name.encode()).hexdigest()[:32],
                     '--mount', 'type=volume,src=' + self.name + ',dst=/cache,volume-nocopy', image,
                     'python3', '-c', 'import time; time.sleep(10**9)'])
        return self.owner()

    def consumers(self, owner):
        ids = self.docker(['ps', '-aq', '--no-trunc', '--filter', 'volume=' + self.name]).split()
        rows = json.loads(self.docker(['inspect', *ids])) if ids else []
        keys = set()
        for row in rows:
            if row['Id'] == owner['Id']:
                continue
            matched = False
            for mount in row.get('HostConfig', {}).get('Mounts', []):
                if mount.get('Source') != self.name:
                    continue
                subpath = mount.get('VolumeOptions', {}).get('Subpath', '')
                if not re.fullmatch(r'objects/[a-f0-9]{64}', subpath):
                    return [], True  # Unknown whole-volume reader: fail closed.
                keys.add(subpath.split('/')[1]);matched = True
            if not matched:
                return [], True
        # An old command stream without a pin still protects the keeper's data.
        return sorted(keys), bool(owner.get('ExecIDs'))

    def execute(self, owner, op, **args):
        consumers, busy = self.consumers(owner)
        request = dict(op=op, owner_id=owner['Id'], started_at=owner['State']['StartedAt'],
                       consumers=consumers, busy=busy, **args)
        return json.loads(self.docker(['exec', self.name, 'python3', '-c', OBJECT_RUNTIME, json.dumps(request)]))

    def resize(self, owner, summary):
        self.summary.update(summary,budget_mb=self.budget())
        memory = 128 + summary['used_mb']
        # Both limits move together; the RAM volume provides the aggregate cap.
        old = owner['HostConfig']['Memory'] // MIB
        options = ['--memory-swap', str(memory) + 'm', '--memory', str(memory) + 'm'] if memory > old else ['--memory', str(memory) + 'm', '--memory-swap', str(memory) + 'm']
        limits = owner['HostConfig']
        if (limits['Memory'] == memory*MIB and limits.get('MemorySwap') == memory*MIB
                and limits.get('NanoCpus') == 1000000000):return
        # The sleeper uses no CPU. Helpers must be able to renew pins and serve
        # metadata promptly even when no object is currently loading.
        self.docker(['update', *options, '--cpus', '1', self.name])

    def dispatch(self, op, args):
        started = time.monotonic()
        with self.lock:
            succeeded = False
            try:
                result = self._dispatch(op,args)
                succeeded = True
            finally:
                elapsed = time.monotonic()-started
                item = self.control_metrics['operations'].setdefault(op,{'calls':0,'errors':0,'total_seconds':0,'max_seconds':0})
                item['calls'] += 1;item['total_seconds'] += elapsed;item['max_seconds'] = max(item['max_seconds'],elapsed)
                item['errors'] += not succeeded
            if op == 'inventory':
                result['control_metrics'] = json.loads(json.dumps(self.control_metrics))
                self.summary['control_metrics'] = result['control_metrics']
                self.summary['sampled_at'] = time.time()
            return result

    def _dispatch(self, op, args):
        with self.lock:
            if not isinstance(args, dict):
                raise ValueError('Expected cache request')
            if op == 'begin':
                if set(args) != {'cache', 'token'}:
                    raise ValueError('Expected cache binding and loading token')
                cache = validate(args['cache'])
                if not self.accepting():
                    raise ValueError('Node is paused')
            elif op == 'prune':
                if set(args) - {'required_mb','keep'} or ('required_mb' in args and (type(args['required_mb']) is not int or args['required_mb'] < 1)):
                    raise ValueError('Invalid reclamation budget')
                if 'keep' in args and (not isinstance(args['keep'],list) or any(not isinstance(k,str) or not re.fullmatch('[a-f0-9]{64}',k) for k in args['keep'])):
                    raise ValueError('Invalid protected cache keys')
            elif op == 'inventory':
                if args:raise ValueError('Inventory accepts no arguments')
            elif op == 'transfer':
                if (set(args) != {'key','transfer_id','direction','bytes'}
                        or args.get('direction') not in ('sent','received')
                        or not isinstance(args.get('transfer_id'),str)
                        or not re.fullmatch('[a-f0-9]{64}',args['transfer_id'])
                        or type(args.get('bytes')) is not int or args['bytes'] < 1):
                    raise ValueError('Invalid cache transfer telemetry')
            elif op in ('check', 'drop'):
                if set(args) != {'key'}:raise ValueError('Expected cache key')
            elif op in ('pin', 'renew', 'release', 'seal'):
                if set(args) != {'key', 'token'}:raise ValueError('Expected cache key and token')
            else:
                raise ValueError('Unknown cache manager operation')
            if 'key' in args and (not isinstance(args['key'], str) or not re.fullmatch('[a-f0-9]{64}', args['key'])):
                raise ValueError('Invalid cache key')
            if 'token' in args and (not isinstance(args['token'], str) or not re.fullmatch('[a-f0-9]{64}', args['token'])):
                raise ValueError('Invalid cache pin')
            owner = self.ensure_owner(cache['image']) if op == 'begin' else self.owner()
            if owner is None or not owner['State']['Running']:
                if op == 'inventory':return {'items': [], 'used_mb': 0, 'budget_mb': self.budget(),
                                           'metrics':{'since':None,'counters':{},'operations':{}}}
                if op == 'prune':return {'removed': [], 'used_mb': 0}
                raise ValueError('Cache miss; prepare object again')
            if op == 'begin':
                inventory = self.execute(owner, 'inventory')
                old = next((i for i in inventory['items'] if i['key'] == cache['key']), None)
                budget = self.budget()
                if cache['shm_mb'] > budget:raise ValueError('Object exceeds node cache budget')
                if old is None:
                    required = max(inventory['used_mb'] + cache['shm_mb'] - budget,
                                   cache['shm_mb'] - int(self.capacity()['available_mb']), 0)
                    if required:
                        pruned = self.execute(owner, 'prune', required_mb=required)
                        self.resize(owner,pruned['summary'])
                        owner = self.owner()
                    if self.capacity()['available_mb'] < cache['shm_mb']:
                        raise ValueError('Cache object awaits node RAM headroom')
                result = self.execute(owner, 'begin', cache=cache, key=cache['key'], token=args['token'], budget_mb=budget)
            else:
                result = self.execute(owner, op, **args)
            # Inventory also reaps abandoned loading objects and upgrades an
            # existing keeper's CPU allowance without restarting it or losing RAM.
            if op in ('begin', 'seal', 'prune', 'drop', 'inventory'):
                self.resize(owner,result['summary'])
            if op == 'inventory':
                result['budget_mb'] = self.budget()
                self.summary = {**result['summary'],'budget_mb':result['budget_mb'],
                                'metrics':result['metrics']}
            return result
