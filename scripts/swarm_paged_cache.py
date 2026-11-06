"""Deploy read-only, kernel demand-paged public artifacts on every enrolled host.

Only explicit public-artifact plans are exported. Private inputs continue to use
verified authenticated transfers. NFS pages live in reclaimable kernel RAM; no
payload file, sparse zero-filled substitute, or complete destination download.
"""
import hashlib
import ipaddress
import json
from pathlib import Path
import re
import subprocess
import time

from scripts.swarm_runtime import AgentClient, CONFIG, atomic_json
from scripts.swarm_shared_cache import contract

PLAN=Path.home()/'.local/state/swarm-control/submission/paged-cache/deployment.json'
SERVICE='paged-reference'


def validate(plan):
    if plan.get('schema')!='jswarm-paged-cache.v1' or plan.get('visibility')!='public':
        raise ValueError('Demand paging requires an explicit public artifact plan')
    key=plan_key(plan)
    if plan['runtime']!=runtime_revision():raise ValueError('Republish the pager runtime before deployment')
    if key!=plan['key']:raise ValueError('Paged artifact identity differs')
    root=Path(plan['export'])
    if not root.is_absolute() or '..' in root.parts or len(root.parts)<4:
        raise ValueError('Use a dedicated immutable artifact directory')
    for name,row in plan['files'].items():
        if Path(name).name!=name or name in ('.','..'):raise ValueError('Invalid public artifact name')
        if Path(row['path'])!=root/name:raise ValueError('Only one dedicated artifact directory may be exported')
    if type(plan['port']) is not int or not 1024<=plan['port']<=65535:raise ValueError('Invalid NFS port')
    ipaddress.IPv4Address(plan['address'])
    if not plan.get('clients'):raise ValueError('No enrolled clients')
    for address in [*plan['clients'].values(),*(a for row in plan.get('aliases',{}).values() for a in row)]:ipaddress.IPv4Address(address)
    return plan


def publish(source,files,image,export,path=None,aliases=None):
    """Application opts its public immutable object set into generic deployment."""
    cfg=json.loads(CONFIG.read_text());runtime=runtime_revision()
    plan={'schema':'jswarm-paged-cache.v1','visibility':'public','key':'','source':source,'runtime':runtime,'aliases':aliases or {},
          'address':cfg['peers'][source],'clients':cfg['peers'],'image':image,'files':files,
          'export':str(Path(export).resolve()),'port':0,'published':time.time()}
    plan['key']=plan_key(plan);plan['port']=21000+int(plan['key'][:8],16)%1000
    validate(plan);path=Path(path or PLAN);path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    atomic_json(path,plan);return plan


def server_config(plan):
    validate(plan)
    clients=','.join(sorted(set(plan['clients'].values())|{plan['address']}|{a for row in plan.get('aliases',{}).values() for a in row}))
    return f'''NFS_CORE_PARAM {{ Protocols = 4; NFS_Port = {plan['port']}; Bind_addr = {plan['address']}; Enable_UDP = false; Enable_NLM = false; Enable_RQUOTA = false; }}
NFSv4 {{ Minor_Versions = 1,2; Grace_Period = 5; Server_Owner = "{plan['key']}"; Server_Scope = "{plan['key']}"; }}
EXPORT {{ Export_Id = 73; Path = /export; Pseudo = /reference;
Protocols = 4; Transports = TCP; Access_Type = None; Squash = All_Squash;
Anonymous_uid = 1000; Anonymous_gid = 1000; SecType = sys;
CLIENT {{ Clients = {clients}; Access_Type = RO; }}
FSAL {{ Name = VFS; }} }}
LOG {{ Default_Log_Level = WARN; Facility {{ name = FILE; destination = /dev/stderr; enable = active; }} }}
'''


# This runs in the source container. It checks immutable identity continuously;
# a changed source closes the export instead of publishing a different artifact.
GUARD=r'''
import json,os,pathlib,subprocess,sys,time
expected=json.loads(sys.argv[1]);paths=[pathlib.Path('/export')/n for n in expected]
def identities():return [(p.stat().st_dev,p.stat().st_ino,p.stat().st_size,p.stat().st_mtime_ns,p.stat().st_ctime_ns) for p in paths]
before=identities()
if before!=[tuple(v) for v in expected.values()]:raise ValueError('Verified source identity changed')
pathlib.Path('/run/ganesha').mkdir(parents=True,exist_ok=True)
pathlib.Path('/var/lib/nfs/ganesha').mkdir(parents=True,exist_ok=True)
pathlib.Path('/run/rpcbind').mkdir(parents=True,exist_ok=True)
subprocess.run(['rpcbind'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,check=False)
p=subprocess.Popen(['ganesha.nfsd','-F','-L','/dev/stderr','-f','/etc/ganesha/ganesha.conf'])
try:
 while p.poll() is None:
  if identities()!=before:raise RuntimeError('Immutable source changed')
  time.sleep(1)
 raise SystemExit(p.returncode)
finally:
 if p.poll() is None:p.terminate();p.wait(timeout=10)
'''

CLIENT=r'''
set -eu
mkdir -p /state/reference /work
mount -t nfs -o retry=0,vers=4.1,port="$2",proto=tcp,ro,nosuid,nodev,noexec,hard,timeo=50,retrans=2,rsize=1048576,nosharecache "$1:/reference" /state/reference
trap 'umount /state/reference 2>/dev/null || true' EXIT
# Mount success, not a complete payload copy, publishes readiness.
touch /work/ready
while [ ! -f /work/stop ]; do sleep 2; done
'''

# Run through the Docker daemon's host bind, never through the API owner's
# private mount namespace. Only the explicitly published directory is exposed.
PUBLIC_FILES=r'''
import hashlib,json,pathlib,sys
files=json.loads(sys.argv[1]);mode=sys.argv[2];prior=json.loads(sys.argv[3]);out={}
for name,record in files.items():
 if pathlib.Path(name).name!=name or name in ('.','..'):raise ValueError('Invalid public artifact name')
 path=pathlib.Path('/export')/name;st=path.stat()
 identity=[st.st_dev,st.st_ino,st.st_size,st.st_mtime_ns,st.st_ctime_ns]
 if st.st_size!=record['bytes']:raise ValueError('Public source size differs')
 if mode=='verify':
  if prior.get(name)!=identity:
   with path.open('rb') as stream:digest=hashlib.file_digest(stream,'sha256').hexdigest()
   if digest!=record['sha256']:raise ValueError('Public source checksum mismatch')
  out[name]=identity
 elif mode=='pages':
  rows=[]
  with path.open('rb') as stream:
   for offset in sorted({0,max(0,st.st_size-4096)}):
    stream.seek(offset);data=stream.read(min(4096,st.st_size-offset))
    rows.append([offset,len(data),hashlib.sha256(data).hexdigest()])
  out[name]=rows
 else:raise ValueError('Invalid public source probe')
 after=path.stat()
 if [after.st_dev,after.st_ino,after.st_size,after.st_mtime_ns,after.st_ctime_ns]!=identity:raise ValueError('Public source changed during verification')
print(json.dumps(out))
'''

# Deployment uses the enrolled host's system daemon even when compute Docker is
# rootless. Mount propagation exposes this one dedicated read-only mount to both.
BOOTSTRAP=r'''
import hashlib,json,os,pathlib,subprocess,sys,time
p=json.load(sys.stdin);plan=p['plan'];image=p['execution_image'];os.umask(0o077)
if hashlib.sha256(pathlib.Path('/etc/machine-id').read_bytes()).hexdigest()!=p['machine']:raise ValueError('Node identity changed')
config=json.loads((pathlib.Path.home()/'.config/lifetech-launcher/peers.json').read_text())
if config['name']!=p['node']:raise ValueError('Node name changed')
docker=['docker','--host','unix:///var/run/docker.sock']
def run(*args,**kw):
 r=subprocess.run(docker+list(args),capture_output=True,text=True,timeout=kw.pop('timeout',60),**kw)
 if r.returncode:raise RuntimeError('Pager deployment command failed: '+r.stderr[-400:])
 return r.stdout
def inspect(name):
 r=subprocess.run(docker+['inspect',name],capture_output=True,text=True,timeout=15)
 if r.returncode:
  if 'no such' in r.stderr.lower():return None
  raise RuntimeError('Cannot inspect pager helper')
 row=json.loads(r.stdout)[0]
 if (row['Config'].get('Labels') or {}).get('org.jswarm.paged-cache')!=plan['key'] or row['Image']!=image:raise ValueError('Pager helper identity differs')
 return row
root=pathlib.Path.home()/'.local/state/swarm-control/paged-cache'/plan['key'];root.mkdir(parents=True,exist_ok=True,mode=0o700)
name='swarm-paged-'+('source-' if p['node']==plan['source'] else 'cache-')+plan['key'][:16]
common=['--restart','unless-stopped','--read-only','--memory','256m','--memory-swap','256m','--pids-limit','64','--network','host','--label','org.lifetech.service=paged-reference','--label','org.jswarm.paged-cache='+plan['key'],'--log-opt','max-size=2m','--log-opt','max-file=2']
if p['node']==plan['source']:
 proof=root/'source-verified.json'
 previous=json.loads(proof.read_text()) if proof.exists() else {}
 identities=json.loads(run('run','--rm','--pull=never','--network','none','--read-only','--cap-drop','ALL',
  '--security-opt','no-new-privileges','--user','1000:1000','--cpus','1','--memory','128m','--memory-swap','128m','--pids-limit','16',
  '--mount','type=bind,src='+plan['export']+',dst=/export,readonly',image,'python3','-c',p['public_files'],
  json.dumps(plan['files']),'verify',json.dumps(previous.get('identities',{})),
  timeout=max(120,60+sum(r['bytes'] for r in plan['files'].values())//(4*1024**2))))
 if previous.get('identities')!=identities:
  proof.write_text(json.dumps({'identities':identities,'verified':time.time()}))
 conf=root/'ganesha.conf'
 if not conf.exists():conf.write_text(p['server_config'])
 elif conf.read_text()!=p['server_config']:raise ValueError('Export policy changed; preserve existing readers and publish a new plan')
 row=inspect(name)
 if not row:
  run('run','-d','--name',name,*common,'--cap-drop','ALL','--cap-add','DAC_READ_SEARCH','--cap-add','NET_BIND_SERVICE','--cap-add','SETUID','--cap-add','SETGID','--cap-add','CHOWN','--cap-add','DAC_OVERRIDE','--security-opt','no-new-privileges','--tmpfs','/run:size=16m','--tmpfs','/tmp:size=16m','--tmpfs','/var/lib/nfs:size=16m','--mount','type=bind,src='+plan['export']+',dst=/export,readonly','--mount','type=bind,src='+str(conf)+',dst=/etc/ganesha/ganesha.conf,readonly',image,'python3','-c',p['guard'],json.dumps(identities))
 elif not row['State']['Running']:run('start',name)
 import socket
 for _ in range(20):
  row=inspect(name)
  if not row['State']['Running'] or row.get('RestartCount',0):raise RuntimeError('Public source did not stay healthy: '+run('logs','--tail','6',name))
  try:
   connection=socket.create_connection((plan['address'],plan['port']),1);connection.close();break
  except OSError:time.sleep(1)
 else:raise RuntimeError('Public source port not ready')
 paths={n:r['path'] for n,r in plan['files'].items()}
else:
 state=root/'mount';state.mkdir(exist_ok=True,mode=0o755);state.chmod(0o755);(state/'reference').mkdir(exist_ok=True,mode=0o755)
 row=inspect(name)
 if not row:
  # Bootstrap only: a dedicated shared mount, never propagation of host root.
  run('run','--rm','--network','none','--pid','host','--privileged',image,'sh','-c','nsenter -t 1 -m -- mountpoint -q "$1" || nsenter -t 1 -m -- mount --bind "$1" "$1"; nsenter -t 1 -m -- mount --make-rshared "$1"','pager',str(state))
  run('run','--rm','--network','none','--cap-drop','ALL','--cap-add','SYS_MODULE','--mount','type=bind,src=/lib/modules,dst=/lib/modules,readonly',image,'modprobe','nfs')
  run('run','-d','--name',name,*common,'--cap-drop','ALL','--cap-add','SYS_ADMIN','--security-opt','apparmor=unconfined','--mount','type=bind,src='+str(state)+',dst=/state,bind-propagation=rshared','--tmpfs','/work:size=1m','--tmpfs','/run:size=1m',image,'sh','-c',p['client'],'pager',plan['address'],str(plan['port']))
 elif not row['State']['Running']:run('start',name)
 for _ in range(30):
  row=inspect(name)
  if not row['State']['Running']:raise RuntimeError('Pager mount failed: '+run('logs','--tail','10',name))
  test=subprocess.run(docker+['exec',name,'test','-f','/work/ready'],capture_output=True,timeout=5)
  if test.returncode==0:break
  time.sleep(1)
 else:raise RuntimeError('Pager mount not ready')
 paths={n:str(state/'reference'/n) for n in plan['files']}
 # Metadata queries must not read payload pages.
 metadata="import json,pathlib;print(json.dumps({n:(pathlib.Path('/state/reference')/n).stat().st_size for n in json.loads(__import__('sys').argv[1])}))"
 sizes=json.loads(run('exec',name,'python3','-c',metadata,json.dumps(list(plan['files']))))
 if any(sizes[n]!=r['bytes'] for n,r in plan['files'].items()):raise ValueError('Mounted object size differs')
mode='local-mmap' if p['node']==plan['source'] else 'nfs-demand-paged'
policy_path=pathlib.Path.home()/'.config/lifetech-launcher/public-cache-policy.json'
policy=json.loads(policy_path.read_text()) if policy_path.exists() else {}
if p['node']!=plan['source'] and policy.get('storage')=='verified-local-ssd':
 import shutil
 local=root/'verified-ssd';local.mkdir(exist_ok=True,mode=0o700)
 # Policy concerns the physical destination filesystem, not the API owner's
 # overlay or private mount view. This helper only inspects host mount metadata.
 rotating=run('run','--rm','--network','none','--pid','host','--privileged',image,'sh','-c',
  'device=$(nsenter -t 1 -m -- findmnt -n -o SOURCE -T "$1"); nsenter -t 1 -m -- lsblk -dn -o ROTA "$device"',
  'pager',str(local)).strip()
 if rotating!='0':raise ValueError('Public cache SSD policy requires verified nonrotating storage')
 for n,expected in plan['files'].items():
  target=local/n;proof=local/(n+'.verified.json')
  prior=json.loads(proof.read_text()) if proof.exists() else {}
  if target.exists():
   st=target.stat();identity=[st.st_dev,st.st_ino,st.st_size,st.st_mtime_ns,st.st_ctime_ns]
   if prior.get('identity')==identity and prior.get('sha256')==expected['sha256'] and st.st_size==expected['bytes']:
    paths[n]=str(target);continue
   raise ValueError('Existing immutable SSD cache identity differs; refusing replacement')
  if shutil.disk_usage(local).free<expected['bytes']+64*1024**2:raise ValueError('Insufficient SSD headroom for public cache')
  temporary=local/(n+'.partial');digest=hashlib.sha256();size=0
  reader=subprocess.Popen(docker+['exec',name,'cat','/state/reference/'+n],stdout=subprocess.PIPE,stderr=subprocess.DEVNULL)
  try:
   with temporary.open('wb') as output:
    while block:=reader.stdout.read(4*1024**2):output.write(block);digest.update(block);size+=len(block)
    output.flush();os.fsync(output.fileno())
   if reader.wait(timeout=30):raise RuntimeError('Public cache reader failed')
  finally:
   if reader.poll() is None:reader.kill();reader.wait()
   reader.stdout.close()
  if size!=expected['bytes'] or digest.hexdigest()!=expected['sha256']:
   temporary.unlink(missing_ok=True);raise ValueError('Public SSD cache checksum differs')
  temporary.chmod(0o444);temporary.replace(target)
  st=target.stat();pending=proof.with_suffix('.tmp')
  pending.write_text(json.dumps({'sha256':expected['sha256'],'identity':[st.st_dev,st.st_ino,st.st_size,st.st_mtime_ns,st.st_ctime_ns]}));pending.replace(proof)
  paths[n]=str(target)
 mode='local-verified-ssd'
record={'schema':'jswarm-paged-node.v1','key':plan['key'],'node':p['node'],'machine':p['machine'],'source':plan['source'],'address':plan['address'],'port':plan['port'],'container':name,'image':image,'paths':paths,'mode':mode,'ready':True,'updated':time.time()}
path=pathlib.Path.home()/'.config/lifetech-launcher/paged-cache.json';pending=path.with_suffix('.tmp');pending.write_text(json.dumps(record));pending.chmod(0o600);pending.replace(path)
print(json.dumps(record))
'''


def host_command(host,code):
    from agent_updates import LauncherUpdates
    return LauncherUpdates.__new__(LauncherUpdates).command(host,code)


def ensure_image(host,image):
    """Reuse Docker image distribution and cross-engine content verification."""
    from docker_utils.images import ImageResolver
    cfg=json.loads(CONFIG.read_text())
    class SystemImages(ImageResolver):
        def command(self,destination,argv):
            target=destination or {'host':cfg['name'],'local':True}
            socket=(destination or {}).get('pager_docker_socket','unix:///var/run/docker.sock')
            code="import os;os.environ['DOCKER_HOST']="+repr(socket)+";os.execvp("+repr(argv[0])+","+repr(argv)+")"
            return host_command(target,code)
    resolver=SystemImages([host]);expected=resolver.fingerprint(None,image)
    if not expected:raise ValueError('Pinned source pager image is unavailable')
    actual=resolver.inspect(host,image)
    if actual and resolver.fingerprint(host,actual)==expected:return actual
    alias='lifetech/paged-cache-helper:'+image.split(':')[1][:20]
    subprocess.run(resolver.command(None,['docker','tag',image,alias]),capture_output=True,check=True,timeout=20)
    resolver.ensure(alias,[host])
    actual=resolver.inspect(host,alias)
    if not actual or resolver.fingerprint(host,actual)!=expected:raise ValueError('Pager image content changed in transfer')
    return actual


def require_system_docker(host):
    """Kernel mounts require rootful Docker, which rootless owners may not expose."""
    code="""import json,subprocess
r=subprocess.run(['docker','--host','unix:///var/run/docker.sock','info','--format','{{json .SecurityOptions}}'],capture_output=True,text=True,timeout=20)
print(json.dumps({'available':r.returncode==0,'rootless':any('rootless' in s for s in json.loads(r.stdout or '[]')) if r.returncode==0 else False}))
"""
    result=subprocess.run(host_command(host,code),capture_output=True,text=True,timeout=30)
    if result.returncode:raise RuntimeError('Cannot inspect the public pager Docker prerequisite through the node API')
    engine=json.loads(result.stdout)
    if not engine['available'] or engine['rootless']:
        raise RuntimeError('Public kernel paging requires system Docker access; verified RAM-cache fallback remains available')


def deploy_node(host,plan=None):
    plan=validate(plan or current_plan())
    if host['host'] not in plan['clients']:raise ValueError('Node is not in the public export allowlist')
    client=AgentClient(host)
    try:status=client.call('status',timeout=15)
    finally:client.close()
    host={**host,'machine':status['machine']}
    require_system_docker(host)
    execution_image=ensure_image(host,plan['image'])
    payload={'plan':plan,'execution_image':execution_image,'node':host['host'],'machine':host['machine'],'guard':GUARD,'client':CLIENT,'public_files':PUBLIC_FILES,'server_config':server_config(plan)}
    result=subprocess.run(host_command(host,BOOTSTRAP),input=json.dumps(payload),capture_output=True,text=True,timeout=max(180,60+sum(r['bytes'] for r in plan['files'].values())//(4*1024**2)))
    if result.returncode:raise RuntimeError('Paged deployment failed on '+host['host']+': '+result.stderr[-500:])
    record=json.loads(result.stdout)
    record['page_checks']=verify_node(host,record,plan)
    record['verified_pages']=True
    if host['host']!=plan['source']:
        try:record['warm_job']=warm_node(host,record,plan,status)
        except (OSError,ValueError,RuntimeError,subprocess.SubprocessError) as error:
            record['warm_job']={'state':'pending','reason':str(error)[-300:]}
    folder=PLAN.parent/'nodes';folder.mkdir(parents=True,exist_ok=True,mode=0o700)
    atomic_json(folder/(host['host']+'.json'),record);return record


def resolve(host,plan):
    """Use only a provisioned, matching node and immutable object identity."""
    validate(plan)
    path=PLAN.parent/'nodes'/(host['host']+'.json')
    if not path.exists():return None
    record=json.loads(path.read_text())
    if record.get('key')!=plan['key'] or record.get('machine')!=host['machine'] or not record.get('ready') or not record.get('verified_pages'):return None
    return record


def deploy_fleet(plan=None,names=None):
    """Standard deployment component: provision source, then every selected node."""
    from concurrent.futures import ThreadPoolExecutor
    plan=validate(plan or current_plan());cfg=json.loads(CONFIG.read_text())
    aliases=discover_aliases(plan)
    if aliases!=plan.get('aliases',{}) or plan['clients']!=cfg['peers']:
        plan=publish(plan['source'],plan['files'],plan['image'],plan['export'],aliases=aliases)
    selected=list(names or cfg['peers'])
    if any(n not in cfg['peers'] for n in selected):raise ValueError('Use enrolled node names')
    def host(name):return {'host':name,'local':name==cfg['name'],'lan_address':cfg['peers'][name],'agent_endpoint':'http://'+cfg['peers'][name]+':8790'}
    rows={}
    try:rows[plan['source']]=deploy_node(host(plan['source']),plan)
    except Exception as error:
        return {n:{'ready':False,'fallback':'verified-complete-cache','reason':'Public source unavailable: '+str(error)[-300:]} for n in selected}
    def target(name):
        try:return name,deploy_node(host(name),plan)
        except Exception as error:return name,{'ready':False,'fallback':'verified-complete-cache','reason':str(error)[-400:]}
    remaining=[n for n in selected if n!=plan['source']]
    if remaining:
        with ThreadPoolExecutor(max_workers=min(4,len(remaining))) as pool:rows.update(pool.map(target,remaining))
    return {n:rows[n] for n in selected}


def runtime_revision():
    import inspect
    return hashlib.sha256((GUARD+CLIENT+PUBLIC_FILES+BOOTSTRAP+inspect.getsource(server_config)).encode()).hexdigest()


def current_plan():
    plan=json.loads(PLAN.read_text())
    if plan.get('visibility')!='public':raise ValueError('Only public artifact plans can be deployed')
    if plan.get('runtime')!=runtime_revision() or plan.get('key')!=plan_key(plan):
        plan=publish(plan['source'],plan['files'],plan['image'],plan['export'],aliases=plan.get('aliases'))
    return validate(plan)


def plan_key(plan):
    object_key,_=contract(plan['image'],plan['files'])
    value={'object':object_key,**{k:plan.get(k) for k in ('runtime','source','address','clients','export')},'aliases':plan.get('aliases',{})}
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':')).encode()).hexdigest()


def discover_aliases(plan):
    """An enrolled node can route from a different LAN IP than its API address."""
    from concurrent.futures import ThreadPoolExecutor
    cfg=json.loads(CONFIG.read_text());network=ipaddress.ip_network(plan['address']+'/24',strict=False)
    def probe(item):
        name,address=item;host={'host':name,'local':name==cfg['name'],'agent_endpoint':'http://'+address+':8790'};client=AgentClient(host)
        try:
            status=client.call('status',timeout=10)
            values=[]
            for raw in status.get('addresses',[]):
                try:ip=ipaddress.ip_address(raw)
                except ValueError:continue
                if ip.version==4 and ip in network:values.append(str(ip))
            return name,sorted(set(values)|{address})
        except Exception:return name,plan.get('aliases',{}).get(name,[address])
        finally:client.close()
    with ThreadPoolExecutor(max_workers=min(5,len(cfg['peers']))) as pool:return dict(pool.map(probe,cfg['peers'].items()))


PROBE=r'''import errno,hashlib,json,mmap,os,pathlib,sys
expected=json.loads(sys.argv[1]);checked=0
for name,probes in expected.items():
 path=pathlib.Path('/reference')/name
 if not os.statvfs(path).f_flag&os.ST_RDONLY:raise ValueError('Demand page mount is not read-only')
 with path.open('rb') as f:
  with mmap.mmap(f.fileno(),0,access=mmap.ACCESS_READ) as mapping:
   for offset,length,digest in probes:
    if hashlib.sha256(mapping[offset:offset+length]).hexdigest()!=digest:raise ValueError('Demand page checksum mismatch')
    checked+=length
 try:
  with path.open('r+b'):pass
 except OSError as error:
  if error.errno not in (errno.EROFS,errno.EACCES,errno.EPERM):raise
 else:raise ValueError('Reference mount is writable')
print(json.dumps({'verified_bytes':checked,'read_only':True,'mmap':True}))
'''


def verify_node(host,record,plan):
    """Real mmap/read-only checks in both system and existing rootless engines."""
    cfg=json.loads(CONFIG.read_text())
    source={'host':plan['source'],'local':plan['source']==cfg['name'],'lan_address':cfg['peers'][plan['source']]}
    source_image=ensure_image(source,plan['image'])
    argv=['docker','--host','unix:///var/run/docker.sock','run','--rm','--pull=never','--network','none','--read-only',
          '--cap-drop','ALL','--security-opt','no-new-privileges','--user','1000:1000','--cpus','0.25',
          '--memory','128m','--memory-swap','128m','--pids-limit','16',
          '--mount','type=bind,src='+plan['export']+',dst=/export,readonly',source_image,
          'python3','-c',PUBLIC_FILES,json.dumps(plan['files']),'pages','{}']
    source_code='import os;os.execvp('+repr(argv[0])+','+repr(argv)+')'
    expected=json.loads(subprocess.check_output(host_command(source,source_code),text=True,timeout=30))
    sockets=json.loads(subprocess.check_output(host_command(host,"import json,os,pathlib;print(json.dumps(['unix:///var/run/docker.sock']+(['unix:///run/user/'+str(os.getuid())+'/docker.sock'] if pathlib.Path('/run/user/'+str(os.getuid())+'/docker.sock').exists() else [])))"),text=True,timeout=20))
    checks=[]
    for socket in sockets:
        engine={**host,'pager_docker_socket':socket};image=record['image'] if socket=='unix:///var/run/docker.sock' else ensure_image(engine,plan['image'])
        # Rootless root maps to the enrolled owner; system jobs use that UID.
        user='0:0' if socket!='unix:///var/run/docker.sock' else '1000:1000'
        argv=['docker','--host',socket,'run','--rm','--pull=never','--network','none','--read-only','--cap-drop','ALL','--security-opt','no-new-privileges','--user',user,'--cpus','0.25','--memory','128m','--memory-swap','128m','--pids-limit','16']
        for name,path in record['paths'].items():argv+=['--mount','type=bind,src='+path+',dst=/reference/'+name+',readonly']
        argv += [image,'python3','-c',PROBE,json.dumps(expected)]
        code='import os;os.execvp('+repr(argv[0])+','+repr(argv)+')'
        result=subprocess.run(host_command(host,code),capture_output=True,text=True,timeout=120)
        if result.returncode:raise RuntimeError('Demand page check failed on '+host['host']+': '+result.stderr[-350:])
        checks.append({'engine':socket,**json.loads(result.stdout)})
    return checks


WARM_CODE=r'''
import hashlib,json,pathlib,sys,time
from launcher_streams import finish
job=json.load(open('/work/job.json'));expected=json.loads(sys.argv[1]);started=time.monotonic();total=0
try:
 for name,record in expected.items():
  digest=hashlib.sha256();size=0
  with (pathlib.Path('/reference')/name).open('rb') as source:
   while block:=source.read(4*1024**2):digest.update(block);size+=len(block)
  if size!=record['bytes'] or digest.hexdigest()!=record['sha256']:raise ValueError('Public cache content differs')
  total+=size
 finish(job,{'verified_bytes':total,'elapsed_seconds':time.monotonic()-started,'cache':'shared reclaimable kernel pages','pinned':False})
except ValueError:raise SystemExit(78)
'''


def warm_definition(record,plan):
    """Owner-installed generic recipe; no private input or arbitrary client path."""
    from scripts.launcher_streams import digest
    expected={n:{'bytes':r['bytes'],'sha256':r['sha256']} for n,r in plan['files'].items()}
    mounts=[{'source':record['paths'][n],'target':'/reference/'+n,'readonly':True} for n in expected]
    row={'image':record['image'],'argv':['python3','-c',WARM_CODE,json.dumps(expected,sort_keys=True)],'mounts':mounts}
    return {**row,'version':digest(row)}


def warm_node(host,record,plan,status=None):
    """Submit once per immutable cache/recipe; the node owns completion/recovery."""
    from scripts.swarm_stream_work import StreamClient
    from scripts.swarm_runtime import shell_command
    from scripts.launcher_streams import digest
    client=AgentClient(host)
    total=(sum(r['bytes'] for r in plan['files'].values())+1024**2-1)//1024**2
    expected={n:{'bytes':r['bytes'],'sha256':r['sha256']} for n,r in plan['files'].items()}
    try:
        status=status or client.call('status',timeout=15)
        if 'node-maintenance-recipes-v1' not in status.get('capabilities',[]):return {'state':'unsupported'}
        if 'verified-ram-caches-v1' not in status.get('capabilities',[]):
            return {'state':'pending','reason':'Node update required before optional reference prefetch'}
        inventory=client.call('resource.caches',timeout=30)
        for cache in inventory['caches']:
            if all(cache['files'].get(name)==row for name,row in expected.items()):
                return {'state':'skipped','reason':'Same immutable objects already verified in shared RAM','cache':cache['key']}
        if inventory.get('pending_caches'):
            return {'state':'skipped','reason':'Native RAM cache fill takes priority over optional prefetch'}
        capacity=client.call('resource.capacity',timeout=15)
        if capacity['available_mb']<total+1024:
            return {'state':'skipped','reason':'Keep reference demand-paged; insufficient spare RAM for optional prefetch'}
    finally:client.close()
    definition=warm_definition(record,plan);recipe='cache.warm.'+plan['key'][:16]
    install="import json,pathlib,os,sys;p=pathlib.Path.home()/'.config/lifetech-launcher/stream-recipes.json';old=json.loads(p.read_text()) if p.exists() else {};old.update(json.loads(sys.argv[1]));t=p.with_suffix('.cache-warm.tmp');t.write_text(json.dumps(old));t.chmod(0o600);os.replace(t,p)"
    subprocess.run(shell_command(host,['python3','-c',install,json.dumps({recipe:definition})]),check=True,capture_output=True,text=True,timeout=30)
    request={'id':'cache-warm-'+digest({'key':plan['key'],'version':definition['version']}),'recipe':recipe,
        'image':record['image'],'inputs':{},'resources':{'cpus':1,'memory_mb':max(256,total+256),'memory_reservation_mb':128},
        'priority':-100,'namespace':'node-cache-maintenance'}
    with StreamClient(host) as streams:row=streams.submit(request)
    return {'id':row['id'],'state':row['state'],'execution_owner':'jswarm-node','priority':-100}
