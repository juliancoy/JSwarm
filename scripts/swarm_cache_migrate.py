"""Retire per-object RAM holders after verified singleton copies and durable rebinding.

Run on the signing controller. Preview is read-only. Apply briefly stops the
follow-up controller and holds its existing plan locks; jobs and results retain
identity. Payloads stream within each node, never through a disk staging file.
"""
import argparse
from contextlib import ExitStack, contextmanager, nullcontext
import fcntl
import json
from pathlib import Path
import sqlite3
import subprocess
import time

from scripts import swarm_shared_cache as caches
from scripts.swarm_runtime import AgentClient, CONFIG, atomic_json

# Runs through the enrolled command channel. Every removal names the observed
# owner instance; Docker independently refuses removal of attached volumes.
RETIRE = r'''
import hashlib,json,pathlib,re,subprocess,sys
p=json.loads(sys.argv[1])
if hashlib.sha256(pathlib.Path('/etc/machine-id').read_bytes()).hexdigest()!=p['machine']:raise ValueError('Machine changed')
def docker(args,missing=False):
 r=subprocess.run(['docker',*args],capture_output=True,text=True,timeout=60)
 if r.returncode:
  if missing and any(v in r.stderr.lower() for v in ('no such','not found')):return None
  raise RuntimeError(r.stderr[-300:])
 return r.stdout
def inspect(kind,name):
 raw=docker([kind,'inspect',name],True)
 return json.loads(raw)[0] if raw is not None else None
out=[]
for expected in p['items']:
 key=expected['key']
 if not re.fullmatch('[a-f0-9]{64}',key):raise ValueError('Invalid key')
 name='swarm-job-'+key[:32];volume='swarm-cache-'+key[:32]
 def checked():
  row=inspect('container',name);v=inspect('volume',volume)
  if (row['Id'] if row else None)!=expected['owner_id']:return None,'owner_changed'
  if row:
   labels=row['Config'].get('Labels') or {}
   if (row['Name'].lstrip('/')!=name or labels.get('org.lifetech.cache-owner')!=key
       or labels.get('org.lifetech.group')!='shared-cache'
       or not any(m.get('Name')==volume and m.get('Destination')=='/dev/shm' and m.get('Type')=='volume' for m in row['Mounts'])):return None,'owner_identity_invalid'
   if row.get('ExecIDs'):return None,'active_exec'
  if not v:return row,'absent'
  opts=v.get('Options') or {}
  if (v['Name']!=volume or v.get('Driver')!='local' or opts.get('type')!='tmpfs'
      or opts.get('device')!='tmpfs' or (v.get('Labels') or {}).get('org.lifetech.cache-owner')!=key):return None,'volume_identity_invalid'
  ids=docker(['ps','-aq','--no-trunc','--filter','volume='+volume]).split()
  if any(ident!=(row['Id'] if row else None) for ident in ids):return None,'docker_consumer'
  return row,None
 row,reason=checked()
 if reason:
  out.append({'key':key,'removed':reason=='absent','reason':reason});continue
 row,reason=checked()
 if reason:
  out.append({'key':key,'removed':False,'reason':reason});continue
 if row:docker(['rm','-f',row['Id']])
 # Revalidate volume identity after owner retirement, without forcing references.
 v=inspect('volume',volume)
 if v:
  if (v.get('Labels') or {}).get('org.lifetech.cache-owner')!=key:raise ValueError('Volume changed')
  docker(['volume','rm',volume])
 out.append({'key':key,'removed':True})
print(json.dumps(out))
'''

COPY = r'''
import json,subprocess,sys
p=json.loads(sys.argv[1]);source=p['source'];target=p['target']
sender=subprocess.Popen(['docker','exec',source['container'],'tar','-C','/dev/shm','-cf','-',*source['files']],stdout=subprocess.PIPE,stderr=subprocess.PIPE)
try:
 receiver=subprocess.run(['docker','exec','-i',target['container'],'python3','-c',p['receive'],target['path'],json.dumps(target['files'])],stdin=sender.stdout,capture_output=True,timeout=300)
 sender.stdout.close();errors=sender.communicate(timeout=30)[1]
 if receiver.returncode or sender.returncode:raise RuntimeError('Cache stream failed: '+(receiver.stderr+errors).decode()[-300:])
finally:
 if sender.poll() is None:sender.kill();sender.wait()
'''


def bindings(value, host=None):
    """Find complete immutable descriptors, preserving physical node identity."""
    if isinstance(value, dict):
        host = value.get('host', host)
        key = value.get('key')
        if isinstance(key, str) and value.get('volume') in ('swarm-cache-'+key[:32], 'swarm-node-cache'):
            caches.validate_binding(value)
            if not isinstance(host, str):raise ValueError('Cache binding has no node')
            yield host.split('#', 1)[0], value
        for item in value.values():yield from bindings(item, host)
    elif isinstance(value, list):
        for item in value:yield from bindings(item, host)


def rebind(value, replacements, host=None):
    if isinstance(value, dict):
        host = value.get('host', host)
        if host and (host.split('#', 1)[0], value.get('key')) in replacements and not caches.is_managed(value):
            replacement = replacements[host.split('#', 1)[0], value['key']]
            caches.validate_binding(value)
            if value['files'] != replacement['files'] or value['image'] != replacement['image']:
                raise ValueError('Replacement differs from immutable binding')
            return {**value, **replacement}
        return {key:rebind(item, replacements, host) for key,item in value.items()}
    if isinstance(value, list):return [rebind(item, replacements, host) for item in value]
    return value


def requests(db):
    return [dict(zip(('namespace','id','state','host','request'), row)) for row in db.execute(
        "SELECT namespace,id,state,host,request FROM submissions WHERE state NOT IN ('completed','failed','cancelled')")]


def references(rows, records, local_host=None):
    found = {}
    def retain(host, cache):
        identity = host, cache['key']
        # A partially completed rebind may mix old and new descriptors for the
        # same bytes. Any remaining legacy reference still needs migration.
        if identity not in found or not caches.is_managed(cache):found[identity] = cache
    for row in rows:
        for host,cache in bindings(json.loads(row['request'] or '{}'), row['host']):retain(host,cache)
    for record in records.values():
        for host,cache in bindings(record, local_host):retain(host,cache)
    return found


def legacy_references(rows, records, local_host=None):
    return {identity:cache for identity,cache in references(rows,records,local_host).items()
            if not caches.is_managed(cache)}


@contextmanager
def quiesce(state, apply):
    if not apply:
        yield
        return
    service = 'genomics-followup.service'
    active = subprocess.run(['systemctl','--user','is-active',service],capture_output=True,text=True).stdout.strip() == 'active'
    try:
        if active:subprocess.run(['systemctl','--user','stop',service],check=True,timeout=60)
        with ExitStack() as locks:
            for path in sorted((state/'genomics-followup').glob('*/controller.lock')):
                stream = locks.enter_context(path.open('a'))
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            yield
    finally:
        if active:subprocess.run(['systemctl','--user','start',service],check=True,timeout=60)


def migrate(state, *, apply=False, output):
    state, output = Path(state).resolve(), Path(output).resolve()
    config = json.loads(CONFIG.read_text())
    report = {'apply':apply, 'started':time.time(), 'nodes':{}, 'rebound':0, 'phase':'validating_sources'}
    def save():atomic_json(output, report)
    save()
    try:
        from agent_updates import deployment_lock
        guard = deployment_lock(operation='legacy cache migration',folder=state) if apply else nullcontext()
        with guard, quiesce(state, apply), ExitStack() as stack:
            db = stack.enter_context(sqlite3.connect((state/'submission/queue.sqlite').as_uri()+('?mode=rw' if apply else '?mode=ro'),uri=True,timeout=30))
            db.execute('PRAGMA synchronous=FULL')
            records = {p:json.loads(p.read_text()) for p in (state/'submission/shared-objects').glob('*.json')}
            rows = requests(db)
            needed = references(rows, records, config['name'])
            hosts = {}
            for name,address in sorted(config['peers'].items()):
                host = {'host':name,'local':False,'agent_route':'auto','agent_endpoint':'http://'+address+':8790'}
                client = AgentClient(host);stack.callback(client.close)
                status = client.call('status',timeout=60)
                if status.get('node') != name or 'node-cache-v1' not in status.get('capabilities',[]):
                    raise ValueError('Node is unavailable or lacks managed cache support: '+name)
                host.update(machine=status['machine'],capabilities=status['capabilities'])
                hosts[name] = (host, client)
                report['nodes'][name] = {'before':client.call('resource.capacity',timeout=60), 'removed':[], 'migrated':[]}
            if any(name not in hosts for name,key in needed):raise ValueError('Unenrolled cache binding')
            # Verify every needed source before removing even unclaimed holders.
            pending = {}
            for (name,key),cache in needed.items():
                host,client = hosts[name]
                if caches.is_managed(cache):
                    try:
                        if apply:
                            client.call('resource.cache.check',{'key':key,'machine':host['machine']},timeout=120)
                            stack.enter_context(caches.pin(host,cache))
                        else:
                            client.call('resource.cache.inventory',{},timeout=120)
                    except (RuntimeError, ValueError):pass  # Existing miss remains rematerializable.
                    continue
                owner = caches.inspect(host,'container',cache['container'])
                if owner and caches.ready(host,cache,owner):pending[name,key] = cache
                else:
                    # A previous interrupted migration may already have sealed it.
                    managed = managed_binding(host, cache)
                    try:
                        if apply:client.call('resource.cache.check',{'key':key,'machine':host['machine']},timeout=120)
                        elif not any(i['key']==key and i['phase']=='ready' for i in client.call('resource.cache.inventory',{},timeout=120)['items']):
                            raise ValueError('Cache miss')
                    except (RuntimeError, ValueError):
                        if any((name,key) in references([r],{},config['name']) for r in rows):
                            raise ValueError('Pending job input has no verified source: '+name+'/'+key)
                        continue  # Stale derived lookup was already a miss.
                    pending[name,key] = cache
            for name,(host,client) in hosts.items():
                budget = client.call('resource.cache.inventory',{},timeout=120)['budget_mb']
                selected = {k:managed_binding(host,c) for (n,k),c in needed.items() if n == name}
                if sum(c['shm_mb'] for c in selected.values()) > budget:
                    raise ValueError('Retained objects exceed node budget: '+name)
            report['required_legacy_objects'] = len(pending)
            save()
            if not apply:
                report['status'] = 'preview'
                return report
            # Retire only objects absent from ALL unfinished queue namespaces and
            # reusable-object metadata. The write lock prevents new claims racing
            # the exact node-local retire command. Recheck before each node batch.
            def retire_unclaimed():
                report['phase'] = 'retiring_unclaimed'
                for name,(host,client) in hosts.items():
                    db.execute('BEGIN IMMEDIATE')
                    try:
                        current_records = {p:json.loads(p.read_text()) for p in (state/'submission/shared-objects').glob('*.json')}
                        retained = legacy_references(requests(db), current_records, config['name'])
                        items = client.call('resource.caches.inspect',{},timeout=120)['items']
                        candidates = [c for c in items if (name,c['key']) not in retained and c['volume_verified'] and c['owner_verified'] and not c['consumers'] and not c['exec_readers']]
                        if candidates:
                            raw = caches.command(host,['python3','-c',RETIRE,json.dumps({'machine':host['machine'],'items':candidates})],timeout=600)
                            report['nodes'][name]['removed'].extend(json.loads(raw))
                    finally:db.rollback()
                    save()
            retire_unclaimed()
            replacements = {}
            report['phase'] = 'copying'
            for (name,key),cache in pending.items():
                host,client = hosts[name]
                def load(owner,path):
                    # Revalidate the legacy seal immediately before the stream.
                    row = caches.inspect(host,'container',cache['container'])
                    if not row or not caches.ready(host,cache,row):raise ValueError('Migration source changed')
                    target = managed_binding(host,cache)
                    caches.command(host,['python3','-c',COPY,json.dumps({'source':cache,'target':target,'receive':RECEIVE})],timeout=360)
                new = caches.ensure(host,cache['image'],cache['files'],load,load_path=True)
                stack.enter_context(caches.pin(host,new))
                replacements[name,key] = new
                report['nodes'][name]['migrated'].append(key)
                save()
            # Back up metadata before switching bindings. Copy verification and
            # seal happen first; interruption leaves either usable representation.
            report['phase'] = 'rebinding'
            backup = output.with_name(output.stem+'-bindings.json')
            atomic_json(backup, {'requests':rows,'records':{str(p):r for p,r in records.items()}})
            for path,record in records.items():
                if json.loads(path.read_text()) != record:
                    raise ValueError('Reusable-object metadata changed during migration')
                updated = rebind(record,replacements,config['name'])
                if updated != record:atomic_json(path,updated)
            db.execute('BEGIN IMMEDIATE')
            try:
                for row in requests(db):
                    old = json.loads(row['request'] or '{}')
                    updated = rebind(old,replacements,row['host'])
                    if updated != old:
                        db.execute('UPDATE submissions SET request=?,updated=? WHERE namespace=? AND id=? AND request IS ?',
                                   (json.dumps(updated),time.time(),row['namespace'],row['id'],row['request']))
                        report['rebound'] += 1
                    folder = old.get('directory')
                    if folder:
                        path = Path(folder)/'transport.json'
                        if path.exists():
                            value = json.loads(path.read_text());changed = rebind(value,replacements,row['host'])
                            if value != changed:atomic_json(path,changed)
                db.commit()
            except BaseException:
                db.rollback();raise
            retire_unclaimed()
            for name,(host,client) in hosts.items():
                node = report['nodes'][name]
                node['remaining_legacy'] = client.call('resource.caches.inspect',{},timeout=120)['items']
                node['managed'] = client.call('resource.cache.inventory',{},timeout=120)
                node['after'] = client.call('resource.capacity',timeout=60)
            report['phase'] = 'finished'
            report['status'] = 'complete' if all(not n['remaining_legacy'] for n in report['nodes'].values()) else 'retained_consumers'
        return report
    except BaseException as error:
        report['status'] = 'error';report['error'] = type(error).__name__+': '+str(error)[:400]
        raise
    finally:
        report['finished'] = time.time();save()


def managed_binding(host, cache):
    key,value = caches.contract(cache['image'],cache['files'])
    size = (sum(f['bytes'] for f in value['files'].values())+1024**2-1)//1024**2+16
    return {**value,'key':key,'host':host['host'],'layout':'node-cache-v1','container':'swarm-node-cache',
            'volume':'swarm-node-cache','path':'/cache/objects/'+key,'subpath':'objects/'+key,'memory_mb':size,'shm_mb':size}


# The bounded receiver rejects extra paths, symlinks, truncation and hash drift.
RECEIVE = r'''
import hashlib,json,os,pathlib,sys,tarfile
root=pathlib.Path(sys.argv[1]);expected=json.loads(sys.argv[2]);seen=set()
with tarfile.open(fileobj=sys.stdin.buffer,mode='r|') as archive:
 for member in archive:
  if member.name not in expected or member.name in seen or not member.isfile():raise ValueError('Unexpected stream member')
  row=expected[member.name]
  if member.size!=row['bytes'] or pathlib.Path(member.name).name!=member.name:raise ValueError('Stream identity differs')
  target=root/member.name;h=hashlib.sha256()
  with archive.extractfile(member) as src,target.open('xb') as dest:
   while chunk:=src.read(1024*1024):dest.write(chunk);h.update(chunk)
  if target.stat().st_size!=row['bytes'] or h.hexdigest()!=row['sha256']:raise ValueError('Stream checksum differs')
  seen.add(member.name)
if seen!=set(expected):raise ValueError('Incomplete stream')
'''


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state',default=str(Path.home()/'.local/state/swarm-control'))
    parser.add_argument('--apply',action='store_true')
    parser.add_argument('--output',required=True)
    args = parser.parse_args(argv)
    result = migrate(args.state,apply=args.apply,output=args.output)
    print(json.dumps({'status':result['status'],'rebound':result['rebound'],'nodes':{
        name:{'migrated':len(row['migrated']),'removed':sum(r['removed'] for r in row['removed']),
              'remaining_legacy':len(row['remaining_legacy']) if 'remaining_legacy' in row else None,'available_mb':row.get('after',{}).get('available_mb')}
        for name,row in result['nodes'].items()}}))
    return int(result['status'] not in ('complete','preview'))


if __name__ == '__main__':raise SystemExit(main())
