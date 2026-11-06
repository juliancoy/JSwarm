"""Stage published node updates through the existing signed owner-command API."""
import hashlib
import ast
import fcntl
import runpy
import json
from pathlib import Path
import shlex
import subprocess
import sys
import time

FILES=('launcher_peers.py','launcher_discovery.py','launcher_registration.py','launcher_agent.py','launcher_registry.py')


def deployment_lock(wait=False):
    """One rolling fleet update, shared by the existing deployment commands."""
    folder=Path.home()/'.local/state/swarm-control'
    folder.mkdir(mode=0o700,parents=True,exist_ok=True)
    lock=(folder/'input-deployment.lock').open('a+')
    try:fcntl.flock(lock,fcntl.LOCK_EX|(0 if wait else fcntl.LOCK_NB))
    except BlockingIOError:
        lock.close()
        raise RuntimeError('A rolling deployment is already running') from None
    return lock


INSTALL = '''import hashlib,json,os,pathlib,runpy,sys,tempfile,subprocess
payload=json.load(sys.stdin)
if hashlib.sha256(pathlib.Path('/etc/machine-id').read_bytes()).hexdigest()!=payload['machine']:
 raise RuntimeError('Update owner differs from the authenticated agent machine')
config=pathlib.Path(os.environ.get('LIFETECH_PEER_CONFIG',str(pathlib.Path.home()/'.config/lifetech-launcher/peers.json')))
settings=json.loads(config.read_text())
if settings['name']!=payload['node']:
 raise RuntimeError('Update command is not running as the enrolled launcher owner')
public_key=payload['controller_public_key']
if len(bytes.fromhex(public_key))!=32:raise RuntimeError('Invalid controller public key')
import ipaddress,re
peers=payload['peers']
if not 1<=len(peers)<=24 or settings['name'] not in peers:raise RuntimeError('Invalid enrolled fleet')
for name,address in peers.items():
 if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,252}',name):raise RuntimeError('Invalid enrolled node')
 ip=ipaddress.ip_address(address)
 if not ip.is_private or ip.is_loopback or ip.is_unspecified:raise RuntimeError('Enrolled LAN address required')
supervisor=payload['supervisor_source']
if hashlib.sha256(supervisor.encode()).hexdigest()!=payload['supervisor_sha256']:raise RuntimeError('Supervisor checksum mismatch')
compile(supervisor,'launcher_peer_supervisor.py','exec')
targets=[pathlib.Path.home()/'.local/share/organization-node/scripts/launcher_peer_supervisor.py']
replacement_containers=[]
# The persistent API command owner may use rootless Docker while the node runs
# on system Docker. Its enrollment-attested routing map still identifies the
# existing writable bootstrap sources without host-PID or SSH access.
routing=config.parent/'bulk-streams.json'
if routing.exists():
 mapped=json.loads(routing.read_text())
 if mapped.get('config',{}).get('machine')!=payload['machine'] or mapped.get('config',{}).get('name')!=settings['name']:
  raise RuntimeError('Bootstrap routing identity differs from enrolled node')
 for mount in mapped.get('host_paths',[]):
  source=pathlib.Path(mount['host'])
  if not source.is_absolute() or '..' in source.parts:raise RuntimeError('Invalid bootstrap source mapping')
  for candidate in (source/'launcher_peer_supervisor.py',source/'scripts/launcher_peer_supervisor.py'):
   if candidate.is_file():targets.append(candidate.resolve())
settings['peers']=dict(peers)
settings['controller_public_key']=public_key
pending=config.with_suffix('.controller.tmp');pending.write_text(json.dumps(settings,indent=2)+'\\n');pending.chmod(0o600);pending.replace(config)
# The launcher hosts coordinator threads and Docker clients for multiple runs.
# Raise undersized provisioned limits without restarting active containers.
# The controller can use system Docker while its jobs use rootless Docker.
# Inspect both endpoints; ownership still requires the enrolled config mount.
for docker_command in (['docker'],['docker','--host','unix:///var/run/docker.sock']):
 for container_name in ('swarm-node','lifetech-launcher','lifetech-launcher-session','organization-test-agent'):
  result=subprocess.run(docker_command+['inspect',container_name],capture_output=True,text=True,timeout=15)
  if result.returncode:continue
  container=json.loads(result.stdout)[0]
  owned=any(m['Destination'] in ('/run/launcher-config',str(config.parent)) and pathlib.Path(m['Source']).resolve()==config.parent.resolve() for m in container.get('Mounts',[]))
  for mount in container.get('Mounts',[]):
   if owned and mount['Destination']=='/session':targets.append(pathlib.Path(mount['Source'])/'launcher_peer_supervisor.py')
   if owned and mount['Destination'].endswith('/scripts') and (pathlib.Path(mount['Source'])/'launcher_peer_supervisor.py').exists():targets.append(pathlib.Path(mount['Source'])/'launcher_peer_supervisor.py')
  if owned:replacement_containers.append((docker_command,container_name))
  current_limit=container['HostConfig'].get('PidsLimit') or 0
  target_limit=min(32768,max(1024,len(os.sched_getaffinity(0))*256+512))
  if owned and 0<current_limit<target_limit:
   subprocess.run(docker_command+['update','--pids-limit',str(target_limit),container_name],check=True,stdout=subprocess.DEVNULL,timeout=20)
for target in targets:
 if not target.exists():continue
 temporary=target.with_suffix('.update.tmp');temporary.write_text(supervisor);temporary.chmod(target.stat().st_mode & 0o777);temporary.replace(target)
# Publish only public TLS routing metadata into the launcher config mount.
bulk_root=pathlib.Path.home()/'.local/state/lifetech-launcher/bulk-transport'
if (bulk_root/'config.json').exists():
 bulk=json.loads((bulk_root/'config.json').read_text())
 if bulk.get('machine')!=payload['machine'] or bulk.get('name')!=settings['name']:raise RuntimeError('Streaming transport identity differs from launcher')
 descriptor=config.parent/'bulk-streams.json';temporary=descriptor.with_suffix('.tmp')
 host_paths=[]
 # Recover owner-attested bind mappings from the foreground standard launcher
 # bootstrap when Docker's live-restore metadata no longer lists the agent.
 for proc in pathlib.Path('/proc').glob('[0-9]*'):
  try:
   if proc.stat().st_uid!=os.getuid():continue
   args=[a.decode() for a in (proc/'cmdline').read_bytes().split(bytes([0])) if a]
   if not args or pathlib.Path(args[0]).name!='docker' or 'run' not in args:continue
   mounts=[]
   for i,arg in enumerate(args[:-1]):
    if arg!='--mount':continue
    mount=dict(part.split('=',1) for part in args[i+1].split(',') if '=' in part)
    if mount.get('type')=='bind' and 'src' in mount and 'dst' in mount:mounts.append(mount)
   if not any(m['src']==str(config.parent) and m['dst'] in (str(config.parent),'/run/launcher-config') for m in mounts):continue
   for mount in mounts:
    source=pathlib.Path(mount['src']);target=pathlib.Path(mount['dst'])
    if source.is_absolute() and target.is_absolute() and '..' not in source.parts and '..' not in target.parts:
     host_paths.append({'container':str(target),'host':str(source)})
  except (OSError,ValueError,UnicodeError):pass
 if not host_paths and descriptor.exists():
  prior=json.loads(descriptor.read_text())
  if prior.get('config',{}).get('machine')!=payload['machine'] or prior.get('config',{}).get('name')!=settings['name']:
   raise RuntimeError('Existing streaming routing identity differs')
  host_paths=prior.get('host_paths',[])
 temporary.write_text(json.dumps({'root':str(bulk_root),'config_dir':str(config.parent),'config':bulk,'host_paths':host_paths}));temporary.chmod(0o600);temporary.replace(descriptor)
files=payload['files']
expected=('launcher_peers.py','launcher_discovery.py','launcher_registration.py','launcher_agent.py','launcher_registry.py')
if set(files)!=set(expected)|{'launcher_update.py'}:
 raise RuntimeError('Incomplete launcher update')
if hashlib.sha256(b''.join(files[name].encode() for name in expected)).hexdigest()!=payload['revision']:
 raise RuntimeError('Launcher update checksum mismatch')
for name,source in files.items():compile(source,name,'exec')
with tempfile.TemporaryDirectory(prefix='launcher-start-update-') as temporary:
 for name,source in files.items():(pathlib.Path(temporary)/name).write_text(source)
 sys.argv=['launcher_update.py']
 runpy.run_path(str(pathlib.Path(temporary)/'launcher_update.py'),run_name='__main__')

# Bridge old supervisors whose in-memory loop still interpreted work counts as
# blockers. The existing admission marker closes starts; Docker keeps the jobs.
replacement_code = "import json,os,pathlib,time\\nconfig=pathlib.Path(os.environ.get('LIFETECH_PEER_CONFIG',str(pathlib.Path.home()/'.config/lifetech-launcher/peers.json')))\\ntarget=(config.parent/'agent-releases/current').resolve()\\nbusy=pathlib.Path('/tmp/launcher-agent-busy.json');gate=pathlib.Path('/tmp/launcher-agent-draining')\\nready=pathlib.Path('/tmp/launcher-agent-ready.json')\\nfor _ in range(300):\\n try:\\n  if json.loads(ready.read_text()).get('folder')==str(target):break\\n except (OSError,ValueError):pass\\n if (config.parent/'agent-releases/current').resolve()!=target:break\\n try:\\n  update=json.loads(pathlib.Path('/tmp/launcher-agent-update.json').read_text())\\n  if update.get('active')==str(target):gate.unlink(missing_ok=True)\\n except (OSError,ValueError):pass\\n try:observed=json.loads(busy.read_text())\\n except (OSError,ValueError):observed={}\\n value={'jobs':[],'browsers':[],'transfers':0,'observed_work':observed.get('observed_work',observed),'workers_survive_restart':True,'time':time.time()}\\n pending=busy.with_suffix('.replace.tmp');pending.write_text(json.dumps(value));pending.replace(busy)\\n time.sleep(.1)\\n"
if replacement_containers:
 for docker_command,container_name in replacement_containers:
  subprocess.run(docker_command+['exec','-d',container_name,'python3','-c',replacement_code],check=True,timeout=15)
  # Legacy supervisors can retain a freshness-based drain loop after their
  # source is replaced. Recreate that bootstrap process using the installed
  # supervisor; independently owned Docker workers remain running.
  probe="import json,pathlib; p=pathlib.Path('/tmp/launcher-agent-update.json'); phase=json.loads(p.read_text()).get('phase','') if p.exists() else ''; print('draining' if phase=='starting' and pathlib.Path('/tmp/launcher-agent-draining').exists() else phase)"
  state=subprocess.run(docker_command+['exec',container_name,'python3','-c',probe],capture_output=True,text=True,timeout=15)
  if state.returncode==0 and state.stdout.strip()=='draining':
   subprocess.run(docker_command+['restart','-t','30',container_name],check=True,stdout=subprocess.DEVNULL,timeout=90)
else:
 subprocess.Popen([sys.executable,'-c',replacement_code],stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,start_new_session=True)
'''


class LauncherUpdates:
    def __init__(self,root,stopped,progress):
        self.stopped,self.progress=stopped,progress
        folder=Path(root)/'scripts'
        self.supervisor_source=(folder/'launcher_peer_supervisor.py').read_text()
        self.files={name:(folder/name).read_text() for name in (*FILES,'launcher_update.py')}
        import base64,gzip
        view=Path(root)/'swarm-control/server/view'
        assets={str(path.relative_to(view)):base64.b64encode(path.read_bytes()).decode() for path in view.rglob('*') if path.is_file()}
        genome=(Path(root).parent if Path(root).name=='JSwarm' else Path(root))/'GenomeHarbor/web'
        assets.update({'genome/'+str(path.relative_to(genome)):base64.b64encode(path.read_bytes()).decode() for path in genome.rglob('*') if path.is_file()})
        encoded=base64.b64encode(gzip.compress(json.dumps(assets,sort_keys=True).encode(),mtime=0)).decode()
        marker="if __name__=='__main__':main()"
        self.files['launcher_peers.py']=self.files['launcher_peers.py'].replace(marker,'PIPELINE_ASSETS_B64 = '+repr(encoded)+'\n\n'+marker)
        runtime=Path(root)/'ramtop/ramtop'
        bundle=runpy.run_path(str(runtime/'bundle.py'))['encoded_runtime'](runtime)
        self.files['launcher_agent.py'] += '\n# Built-in monitor source is covered by the existing atomic release checksum.\nPERFORMANCE_RUNTIME_B64 = '+repr(bundle)+'\n'
        self.files['launcher_agent.py'] += '\n# Durable streaming runtime is part of this same verified launcher release.\nSTREAM_RUNTIME_SOURCE = '+repr((folder/'launcher_streams.py').read_text())+'\n'
        self.files['launcher_agent.py'] += '\n# Signed command runtime shares this same verified release.\nCOMMAND_RUNTIME_SOURCE = '+repr((folder/'launcher_commands.py').read_text())+'\n'
        self.files['launcher_agent.py'] += '\n# The supervisor version is part of the published release identity.\nSUPERVISOR_SHA256 = '+repr(hashlib.sha256(self.supervisor_source.encode()).hexdigest())+'\n'
        self.capabilities=set()
        for node in ast.walk(ast.parse(self.files['launcher_agent.py'])):
            if isinstance(node,ast.Dict):
                for key,value in zip(node.keys,node.values):
                    if isinstance(key,ast.Constant) and key.value=='capabilities' and isinstance(value,ast.List):
                        self.capabilities.update(item.value for item in value.elts if isinstance(item,ast.Constant) and isinstance(item.value,str))
        self.revision=hashlib.sha256(b''.join(self.files[name].encode() for name in FILES)).hexdigest()
        from deployment_components import snapshot
        self.components=snapshot(root)

    @classmethod
    def published(cls,path,stopped,progress):
        """Load a complete immutable release, never reread mutable workspace files."""
        value=json.loads(Path(path).read_text())
        if value.get('schema')!='jswarm-agent-release.v1':raise ValueError('Unsupported published release schema')
        files=value['files'];expected=set(FILES)|{'launcher_update.py'}
        if set(files)!=expected:raise ValueError('Incomplete published agent release')
        revision=hashlib.sha256(b''.join(files[name].encode() for name in FILES)).hexdigest()
        if revision!=value['revision']:raise ValueError('Published agent release checksum mismatch')
        supervisor=value['supervisor_source']
        if hashlib.sha256(supervisor.encode()).hexdigest()!=value['supervisor_sha256']:raise ValueError('Published supervisor checksum mismatch')
        for name,source in {**files,'launcher_peer_supervisor.py':supervisor}.items():compile(source,name,'exec')
        instance=cls.__new__(cls);instance.stopped=stopped;instance.progress=progress
        instance.files=files;instance.revision=revision;instance.supervisor_source=supervisor;instance.capabilities=set()
        from deployment_components import validate
        instance.components=value.get('components',{})
        if set(instance.components)-{'public-pager'}:raise ValueError('Unknown deployment component')
        for component in instance.components.values():validate(component)
        for node in ast.walk(ast.parse(files['launcher_agent.py'])):
            if isinstance(node,ast.Dict):
                for key,entry in zip(node.keys,node.values):
                    if isinstance(key,ast.Constant) and key.value=='capabilities' and isinstance(entry,ast.List):
                        instance.capabilities.update(item.value for item in entry.elts if isinstance(item,ast.Constant) and isinstance(item.value,str))
        return instance

    def publish(self,folder):
        """Publish checksummed sources only; enrolled identities and keys stay separate."""
        import os,tempfile
        folder=Path(folder);folder.mkdir(parents=True,exist_ok=True,mode=0o700)
        value={'schema':'jswarm-agent-release.v1','revision':self.revision,'files':self.files,
               'supervisor_source':self.supervisor_source,'supervisor_sha256':hashlib.sha256(self.supervisor_source.encode()).hexdigest(),
               'components':getattr(self,'components',{})}
        # Supervisor is included in the artifact name so it cannot silently change.
        artifact=hashlib.sha256(json.dumps(value,sort_keys=True).encode()).hexdigest()
        path=folder/(artifact+'.json');encoded=json.dumps(value,sort_keys=True)
        with tempfile.NamedTemporaryFile(dir=folder,mode='w',delete=False) as handle:
            temp=Path(handle.name);handle.write(encoded);handle.flush();os.fsync(handle.fileno())
        try:
            temp.chmod(0o600)
            try:os.link(temp,path)
            except FileExistsError:
                if path.read_text()!=encoded:raise ValueError('Immutable release content changed')
        finally:temp.unlink(missing_ok=True)
        self.published(path,self.stopped,self.progress)
        pointer=folder/'desired'
        if pointer.is_symlink() and pointer.resolve()!=path.resolve():
            prior=folder/'previous.tmp';prior.unlink(missing_ok=True)
            prior.symlink_to(pointer.resolve().name);prior.replace(folder/'previous')
        pending=folder/'desired.tmp';pending.unlink(missing_ok=True)
        pending.symlink_to(path.name);pending.replace(pointer)
        directory=os.open(folder,os.O_RDONLY)
        try:os.fsync(directory)
        finally:os.close(directory)
        return path

    @classmethod
    def desired(cls,stopped=lambda:False,progress=lambda *args:None):
        """Deployment entry points consume the same validated fleet publication."""
        return cls.published(Path.home()/'.local/state/swarm-control/node-releases/desired',stopped,progress)

    @classmethod
    def rollback(cls,folder):
        """Select a verified previous publication for the normal rolling updater."""
        import os
        folder=Path(folder);previous=folder/'previous';desired=folder/'desired'
        target=previous.resolve(strict=True)
        if target.parent!=folder.resolve() or not previous.is_symlink():
            raise ValueError('Previous release must be a local published artifact')
        release=cls.published(target,lambda:False,lambda *args:None)
        current=desired.resolve(strict=True)
        if current.parent!=folder.resolve():raise ValueError('Desired release escapes publication directory')
        cls.published(current,lambda:False,lambda *args:None)
        pending=folder/'desired.tmp';pending.unlink(missing_ok=True)
        pending.symlink_to(target.name);pending.replace(desired)
        pending=folder/'previous.tmp';pending.unlink(missing_ok=True)
        pending.symlink_to(current.name);pending.replace(previous)
        directory=os.open(folder,os.O_RDONLY)
        try:os.fsync(directory)
        finally:os.close(directory)
        return release

    def owner_status(self,host):
        """Verify the enrolled node through its API; deployment never falls back to SSH."""
        from agent_client import AgentClient,CONFIG
        settings=json.loads(CONFIG.read_text())
        if host['host'] not in settings['peers']:raise ValueError('Use an enrolled node name')
        endpoint=host.get('agent_endpoint') or 'http://'+settings['peers'][host['host']]+':8790'
        client=AgentClient({**host,'agent_endpoint':endpoint})
        try:status=client.call('status',timeout=10)
        finally:client.close()
        if status.get('node')!=host['host'] or not isinstance(status.get('machine'),str) or len(bytes.fromhex(status['machine']))!=32:
            raise ValueError('Update API identity differs from enrolled node')
        if host.get('machine') and host['machine']!=status['machine']:raise ValueError('Update machine identity changed')
        return status

    def stage(self,host,status):
        if self.stopped():raise InterruptedError('Launcher update stopped')
        from agent_client import CONFIG
        public_key=json.loads(CONFIG.read_text())['controller_public_key']
        if len(bytes.fromhex(public_key))!=32:raise ValueError('Invalid pinned controller key')
        payload={'supervisor_source':self.supervisor_source,'supervisor_sha256':hashlib.sha256(self.supervisor_source.encode()).hexdigest(),'machine':status['machine'],'node':status['node'],'revision':self.revision,'files':self.files,'controller_public_key':public_key,'peers':json.loads(CONFIG.read_text())['peers']}
        command=self.command(host,INSTALL,status=status)
        try:result=subprocess.run(command,input=json.dumps(payload),capture_output=True,text=True,timeout=180)
        except subprocess.TimeoutExpired:raise RuntimeError('Launcher staging timed out; existing jobs preserved and retry is safe') from None
        if result.returncode:
            # No source payload, fleet keys or account configuration enter run logs.
            raise RuntimeError('Launcher staging failed through the signed node API: '+result.stderr[-400:])

    def command(self,host,code,*,status=None):
        """Run in the enrolled owner's persistent command service over the node API.

        Command IDs and acknowledged byte windows survive agent replacement.
        An unavailable API is a pending deployment, never permission to switch
        transport or to act on another machine using ambient SSH configuration.
        """
        from agent_client import CONFIG
        from scripts.swarm_runtime import shell_command
        settings=json.loads(CONFIG.read_text());name=host['host']
        if name not in settings['peers']:raise ValueError('Use an enrolled node name')
        status=self.owner_status(host) if status is None else status
        if status.get('node')!=name or not isinstance(status.get('machine'),str) or len(bytes.fromhex(status['machine']))!=32:
            raise ValueError('Update API identity differs from enrolled node')
        if host.get('machine') and host['machine']!=status['machine']:raise ValueError('Update machine identity changed')
        if 'node-command-v1' not in status.get('capabilities',[]):
            raise RuntimeError('Signed node command API unavailable; enroll or recover the node explicitly')
        owner={**host,'local':False,'machine':status['machine'],
               'agent_endpoint':host.get('agent_endpoint') or 'http://'+settings['peers'][name]+':8790'}
        return shell_command(owner,['python3','-c',code])

    def cancel_pending(self,host,status):
        """Undo only our staged pointer, preserving the existing node and jobs."""
        code=r"""import hashlib,json,os,pathlib,sys
p=json.load(sys.stdin)
if hashlib.sha256(pathlib.Path('/etc/machine-id').read_bytes()).hexdigest()!=p['machine']:raise ValueError('Machine identity differs')
config=pathlib.Path(os.environ.get('LIFETECH_PEER_CONFIG',str(pathlib.Path.home()/'.config/lifetech-launcher/peers.json')))
if json.loads(config.read_text())['name']!=p['node']:raise ValueError('Node identity differs')
base=config.parent/'agent-releases';current=base/'current';previous=base/'previous'
if current.resolve().name!=p['desired'] or not previous.is_symlink() or previous.resolve().name!=p['active']:raise ValueError('Release pointer changed; cancellation refused')
pending=base/'cancel.tmp';pending.unlink(missing_ok=True);pending.symlink_to(previous.resolve().name);pending.replace(current)
"""
        payload={'node':host['host'],'machine':status['machine'],'desired':self.revision,'active':status['source_revision']}
        result=subprocess.run(self.command(host,code,status=status),input=json.dumps(payload),capture_output=True,text=True,timeout=30)
        if result.returncode:raise RuntimeError('Cannot safely cancel staged node update')

    def ensure(self,host,client,status,allow_compatible=False,drain=False,wait_timeout=240):
        if status.get('source_revision')==self.revision and status.get('ready'):return status
        removed=set(status.get('capabilities',[]))-self.capabilities
        if removed:
            if not status.get('ready'):raise RuntimeError('Launcher update would remove capabilities: '+', '.join(sorted(removed)))
            host['launcher_update']={'status':'deferred','from_revision':status.get('source_revision'),'to_revision':self.revision,'reason':'Update would remove launcher capabilities: '+', '.join(sorted(removed))}
            return status
        if allow_compatible and status.get('ready'):
            host['launcher_update']={'status':'deferred','from_revision':status.get('source_revision'),
                                     'to_revision':self.revision,'reason':'Compatible running agent; updates are managed separately from task readiness'}
            return status
        identity=status['machine']
        detail={'status':'updating','verified':False,'from_revision':status.get('source_revision'),'to_revision':self.revision}
        host['session']=detail
        self.progress(host,'updating',detail)
        self.stage(host,status)
        self.progress(host,'waiting_for_update',{**detail,'status':'waiting_for_update'})
        deadline=time.monotonic()+wait_timeout
        while time.monotonic()<deadline:
            if self.stopped():raise InterruptedError('Launcher update stopped')
            try:value=client.call('status',timeout=5)
            except Exception:value=None  # Connection loss is expected during supervisor reload.
            if value:
                if value.get('machine')!=identity:raise RuntimeError('Machine identity changed during launcher update')
                if value.get('source_revision')==self.revision and value.get('ready'):
                    host['launcher_update']={'status':'updated','from_revision':status.get('source_revision'),'to_revision':self.revision}
                    return value
            time.sleep(.5)
        raise RuntimeError('Launcher update did not become ready; supervisor may have rolled back. No tests dispatched to this machine')
