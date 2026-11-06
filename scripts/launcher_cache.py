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
 state=root/'.state.json';now=time.time()
 entries=json.loads(state.read_text()) if state.exists() else {}
 for item in entries.values():item['pins']={k:v for k,v in item['pins'].items() if v>now}
 def save():
  tmp=root/'.state.tmp';tmp.write_text(json.dumps(entries));os.replace(tmp,state)
 def used():return sum(i['cache']['shm_mb'] for i in entries.values())
 def protected(key,item):return bool(item['pins']) or key in p.get('consumers',[]) or key in p.get('keep',[]) or p.get('busy',False)
 def drop(key):
  item=entries.get(key)
  if item is None:return {'removed':False,'reason':'absent'}
  if protected(key,item):return {'removed':False,'reason':'pinned'}
  path=root/'objects'/key
  if path.is_symlink():raise ValueError('Cache directory changed')
  if path.exists():
   path.chmod(0o700);shutil.rmtree(path)
  del entries[key]
  return {'removed':True,'key':key}
 op=p['op'];key=p.get('key');result={}
 if key is not None and not re.fullmatch('[a-f0-9]{64}',key):raise ValueError('Invalid object key')
 if op=='inventory':
  result={'items':[dict(i['cache'],phase=i['phase'],pinned=protected(k,i),last_used=i['last_used']) for k,i in entries.items()], 'used_mb':used()}
 elif op=='prune':
  removed=[]
  for k,i in sorted(list(entries.items()),key=lambda kv:kv[1]['last_used']):
   if p.get('required_mb') is not None and sum(r['shm_mb'] for r in removed)>=p['required_mb']:break
   r=drop(k)
   if r['removed']:removed.append(i['cache'])
  result={'removed':removed,'used_mb':used()}
 elif op=='drop':result=drop(key)
 elif op=='begin':
  item=entries.get(key)
  if item and item['phase']=='ready':
   item['last_used']=now;result={'state':'ready','cache':item['cache']}
  elif item and protected(key,item):result={'state':'waiting'}
  else:
   if item:drop(key)
   cache=p['cache']
   if used()+cache['shm_mb']>p['budget_mb']:result={'state':'capacity'}
   else:
    path=root/'objects'/key;path.mkdir(parents=True,mode=0o700)
    entries[key]={'cache':cache,'phase':'loading','last_used':now,'pins':{p['token']:now+120}}
    result={'state':'loading','cache':cache}
 elif op in ('pin','renew','release','seal','check'):
  item=entries.get(key)
  if item is None:raise ValueError('Cache miss; prepare object again')
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
  else:
   if item['phase']!='ready':raise ValueError('Cache miss; prepare object again')
   proof=json.loads((path/'.ready.json').read_text())
   if proof['container_id']!=p['owner_id'] or proof['started_at']!=p['started_at']:raise ValueError('Cache keeper changed')
   for name,identity in proof['identities'].items():
    f=path/name;s=f.lstat()
    if not stat.S_ISREG(s.st_mode) or s.st_mode&0o222 or [s.st_dev,s.st_ino,s.st_size,s.st_mtime_ns]!=identity:raise ValueError('Cache file changed')
   item['last_used']=now
  result={'cache':item['cache'],'used_mb':used()}
 else:raise ValueError('Unknown cache operation')
 save();print(json.dumps(result))
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
    def __init__(self, *, capacity, machine, accepting=lambda: True, run=subprocess.run, name=NAME):
        self.capacity, self.machine, self.accepting, self.run = capacity, machine, accepting, run
        # A custom name is for isolated Docker integration tests only.
        self.name = name
        self.lock = threading.RLock()
        self.summary = {}

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
        total = int(self.capacity()['memory_mb'])
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
                     '--cpus', '0.1', '--pids-limit', '128', '--label', LABEL + '=' + self.machine,
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

    def resize(self, owner):
        inventory = self.execute(owner, 'inventory')
        self.summary = {'objects':len(inventory['items']), 'pinned_objects':sum(i['pinned'] for i in inventory['items']),
                        'used_mb':inventory['used_mb'], 'budget_mb':self.budget()}
        memory = 128 + inventory['used_mb']
        loading = any(i['phase'] == 'loading' for i in inventory['items'])
        # Both limits move together; the RAM volume provides the aggregate cap.
        old = owner['HostConfig']['Memory'] // MIB
        options = ['--memory-swap', str(memory) + 'm', '--memory', str(memory) + 'm'] if memory > old else ['--memory', str(memory) + 'm', '--memory-swap', str(memory) + 'm']
        self.docker(['update', *options, '--cpus', '1' if loading else '0.1', self.name])

    def dispatch(self, op, args):
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
                if op == 'inventory':return {'items': [], 'used_mb': 0, 'budget_mb': self.budget()}
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
                        self.execute(owner, 'prune', required_mb=required)
                        self.resize(owner)
                    if self.capacity()['available_mb'] < cache['shm_mb']:
                        raise ValueError('Cache object awaits node RAM headroom')
                result = self.execute(owner, 'begin', cache=cache, key=cache['key'], token=args['token'], budget_mb=budget)
            else:
                result = self.execute(owner, op, **args)
            if op in ('begin', 'seal', 'prune', 'drop'):
                self.resize(owner)
            if op == 'inventory':
                result['budget_mb'] = self.budget()
                self.summary = {'objects':len(result['items']), 'pinned_objects':sum(i['pinned'] for i in result['items']),
                                'used_mb':result['used_mb'], 'budget_mb':result['budget_mb']}
            return result
