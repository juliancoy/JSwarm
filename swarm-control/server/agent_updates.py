"""Stage published node updates through the existing signed owner-command API."""
import hashlib
import ast
import fcntl
import json
from pathlib import Path
import shlex
import subprocess
import sys
import time

FILES=('launcher_peers.py','launcher_discovery.py','launcher_registration.py','launcher_agent.py','launcher_registry.py')


from contextlib import contextmanager
import os
import socket
import threading

_LOCK_STATE=threading.local()
_UNSPECIFIED=object()


@contextmanager
def deployment_lock(wait=False,*,operation='deployment',folder=None):
    """One process owns validation, publication or rollout; the kernel releases crashes."""
    folder=Path(folder) if folder is not None else Path.home()/'.local/state/swarm-control'
    folder.mkdir(mode=0o700,parents=True,exist_ok=True)
    path=(folder/'input-deployment.lock').resolve()
    held=getattr(_LOCK_STATE,'held',{})
    if held.get(path)==os.getpid():
        yield
        return
    lock=path.open('a+')
    try:
        try:fcntl.flock(lock,fcntl.LOCK_EX|(0 if wait else fcntl.LOCK_NB))
        except BlockingIOError:
            lock.seek(0)
            try:owner=json.load(lock);detail=f"{owner['operation']} on {owner['host']} (PID {owner['pid']})"
            except (ValueError,KeyError,TypeError):detail='another deployment process'
            raise RuntimeError('Deployment locked by '+detail+'; retry after it finishes') from None
        os.fchmod(lock.fileno(),0o600)
        lock.seek(0);lock.truncate()
        json.dump({'pid':os.getpid(),'host':socket.gethostname(),'operation':operation,'started_at':time.time()},lock)
        lock.flush()
        held[path]=os.getpid();_LOCK_STATE.held=held
        try:yield
        finally:held.pop(path,None)
    finally:lock.close()


def publication_token(folder):
    """Capture the exact desired artifact, including its deployment generation."""
    pointer=Path(folder)/'desired'
    if not pointer.is_symlink():
        if pointer.exists():raise ValueError('Desired release must be an atomic publication pointer')
        return ''
    target=pointer.resolve(strict=True)
    if target.parent!=Path(folder).resolve():raise ValueError('Desired release escapes publication directory')
    return target.name


def check_publication(folder,expected):
    if publication_token(folder)!=expected:
        raise RuntimeError('Desired release advanced during preparation; validate and publish the current source again')


def require_git_source(source):
    """All deployable publications must carry validated Git provenance."""
    if not isinstance(source,dict) or set(source)!={'repositories','installer','mode','activation'}:
        raise ValueError('Git source provenance required; publish committed source with --publish')
    from scripts.node_git_source import validate
    validate(source['repositories'])
    compile(source['installer'],'node_git_source.py','exec')
    if source['mode']!='mounted-v1':raise ValueError('Unsupported node source mode')
    compile(source['activation'],'node_source_install.py','exec')


def source_revision(files, git_source):
    data=b''.join(files[name].encode() for name in FILES)
    data+=json.dumps(git_source['repositories'],sort_keys=True).encode()
    return hashlib.sha256(data).hexdigest()


class LauncherUpdates:
    def __init__(self,root,stopped,progress):
        self.stopped,self.progress=stopped,progress
        root=Path(root)
        if (root/'JSwarm/scripts').is_dir():root=root/'JSwarm'
        folder=Path(root)/'scripts'
        manifest=Path(root)/'.node-git-source.json'
        from scripts.node_git_source import snapshot,validate
        repositories=json.loads(manifest.read_text()) if manifest.exists() else snapshot(root.parent)
        validate(repositories)
        self.git_source={'repositories':repositories,'installer':(folder/'node_git_source.py').read_text(),
                         'mode':'mounted-v1','activation':(folder/'node_source_install.py').read_text()}
        self.supervisor_source=(folder/'launcher_peer_supervisor.py').read_text()
        self.files={name:(folder/name).read_text() for name in FILES}
        self.capabilities=set()
        for node in ast.walk(ast.parse(self.files['launcher_agent.py'])):
            if isinstance(node,ast.Dict):
                for key,value in zip(node.keys,node.values):
                    if isinstance(key,ast.Constant) and key.value=='capabilities' and isinstance(value,ast.List):
                        self.capabilities.update(item.value for item in value.elts if isinstance(item,ast.Constant) and isinstance(item.value,str))
        self.revision=source_revision(self.files,self.git_source)
        from deployment_components import snapshot
        self.components=snapshot(root)

    @classmethod
    def published(cls,path,stopped,progress):
        """Load a deployable Git-backed publication."""
        instance=cls._read_publication(path,stopped,progress)
        require_git_source(instance.git_source)
        from deployment_components import validate
        for component in instance.components.values():validate(component)
        return instance

    @classmethod
    def _read_publication(cls,path,stopped,progress):
        """Read only the current mounted-source publication format."""
        path=Path(path).resolve(strict=True)
        value=json.loads(path.read_text())
        if value.get('schema')!='jswarm-mounted-release.v1':raise ValueError('Unsupported published release schema')
        files=value['files'];expected=set(FILES)
        if set(files)!=expected:raise ValueError('Incomplete published agent release')
        require_git_source(value.get('git_source'))
        revision=source_revision(files,value['git_source'])
        if revision!=value['revision']:raise ValueError('Published agent release checksum mismatch')
        supervisor=value['supervisor_source']
        if hashlib.sha256(supervisor.encode()).hexdigest()!=value['supervisor_sha256']:raise ValueError('Published supervisor checksum mismatch')
        for name,source in {**files,'launcher_peer_supervisor.py':supervisor}.items():compile(source,name,'exec')
        instance=cls.__new__(cls);instance.stopped=stopped;instance.progress=progress
        instance.files=files;instance.revision=revision;instance.supervisor_source=supervisor;instance.capabilities=set()
        instance.publication_folder=path.parent
        instance.publication_artifact=path.name
        from deployment_components import validate
        instance.generation=value.get('generation',0)
        if type(instance.generation) is not int or instance.generation<0:raise ValueError('Invalid deployment generation')
        instance.source_versions=value.get('source_versions',{instance.revision:instance.generation})
        if (not isinstance(instance.source_versions,dict) or instance.revision not in instance.source_versions or any(not isinstance(key,str) or type(version) is not int or not 0<=version<=instance.generation for key,version in instance.source_versions.items())):
            raise ValueError('Invalid source version history')
        instance.git_source=value['git_source']
        instance.components=value.get('components',{})
        if set(instance.components)-{'public-pager','private-filesystem'}:raise ValueError('Unknown deployment component')
        for component in instance.components.values():validate(component)
        content={key:value.get(key,{}) if key=='components' else value[key] for key in ('schema','revision','files','supervisor_source','supervisor_sha256','components')}
        if 'git_source' in value:content['git_source']=value['git_source']
        content_id=hashlib.sha256(json.dumps(content,sort_keys=True).encode()).hexdigest()
        instance.artifact_versions=value.get('artifact_versions',{content_id:instance.generation})
        if (not isinstance(instance.artifact_versions,dict) or content_id not in instance.artifact_versions or any(type(version) is not int or not 0<=version<=instance.generation for version in instance.artifact_versions.values())):
            raise ValueError('Invalid artifact version history')
        for node in ast.walk(ast.parse(files['launcher_agent.py'])):
            if isinstance(node,ast.Dict):
                for key,entry in zip(node.keys,node.values):
                    if isinstance(key,ast.Constant) and key.value=='capabilities' and isinstance(entry,ast.List):
                        instance.capabilities.update(item.value for item in entry.elts if isinstance(item,ast.Constant) and isinstance(item.value,str))
        return instance

    def publish(self,folder,*,expected=_UNSPECIFIED):
        folder=Path(folder).resolve()
        with deployment_lock(folder=folder.parent,operation='publish release'):
            if expected is not _UNSPECIFIED:check_publication(folder,expected)
            return self._publish(folder)

    def _publish(self,folder,*,rollback=False):
        """Publish checksummed sources only; enrolled identities and keys stay separate."""
        import os,tempfile
        folder=Path(folder);folder.mkdir(parents=True,exist_ok=True,mode=0o700)
        value={'schema':'jswarm-mounted-release.v1','revision':self.revision,'files':self.files,
               'supervisor_source':self.supervisor_source,'supervisor_sha256':hashlib.sha256(self.supervisor_source.encode()).hexdigest(),
               'components':getattr(self,'components',{})}
        require_git_source(self.git_source)
        value['git_source']=self.git_source
        # Content identity stays stable; the immutable envelope orders deployments.
        artifact=hashlib.sha256(json.dumps(value,sort_keys=True).encode()).hexdigest()
        token=publication_token(folder)
        current=folder/token if token else None
        versions={};artifacts={}
        prior=None
        # Unsupported prior artifacts remain opaque audit history, never executable rollback targets.
        if current and json.loads(current.read_text()).get('schema')==value['schema']:
            prior=self._read_publication(current,self.stopped,self.progress)
            prior_value=json.loads(current.read_text())
            content={key:prior_value.get(key,{}) if key=='components' else prior_value[key]
                     for key in ('schema','revision','files','supervisor_source','supervisor_sha256','components')}
            if 'git_source' in prior_value:content['git_source']=prior_value['git_source']
            current_id=hashlib.sha256(json.dumps(content,sort_keys=True).encode()).hexdigest()
            versions=dict(prior.source_versions);artifacts=dict(prior.artifact_versions)
            if content==value:
                self.generation=prior.generation
                self.source_versions=versions
                self.artifact_versions=artifacts
                self.publication_folder=folder;self.publication_artifact=current.name
                return current
            if not rollback and self.revision in versions and versions[self.revision]<versions[prior.revision]:
                raise RuntimeError('Older source version requires explicit rollback')
        if not rollback and artifact in artifacts and artifacts[artifact]<artifacts[current_id]:
            raise RuntimeError('Previously published release requires explicit rollback')
        generations=[int(path.name.split('-',1)[0]) for path in folder.glob('*-*.json')
                     if path.name.split('-',1)[0].isdigit()]
        generation=max([prior.generation if prior else 0,*generations])+1
        versions.setdefault(self.revision,generation);artifacts.setdefault(artifact,generation)
        value.update(generation=generation,predecessor=token,source_versions=versions,artifact_versions=artifacts)
        path=folder/(f'{generation:020d}-'+artifact+'.json');encoded=json.dumps(value,sort_keys=True)
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
        if prior and pointer.is_symlink() and pointer.resolve()!=path.resolve():
            prior=folder/'previous.tmp';prior.unlink(missing_ok=True)
            prior.symlink_to(pointer.resolve().name);prior.replace(folder/'previous')
        if prior is None:(folder/'previous').unlink(missing_ok=True)
        pending=folder/'desired.tmp';pending.unlink(missing_ok=True)
        pending.symlink_to(path.name);pending.replace(pointer)
        directory=os.open(folder,os.O_RDONLY)
        try:os.fsync(directory)
        finally:os.close(directory)
        self.generation=generation
        self.source_versions=versions
        self.artifact_versions=artifacts
        self.publication_folder=folder;self.publication_artifact=path.name
        return path

    @classmethod
    def desired(cls,stopped=lambda:False,progress=lambda *args:None):
        """Deployment entry points consume the same validated fleet publication."""
        return cls.published(Path.home()/'.local/state/swarm-control/node-releases/desired',stopped,progress)

    @classmethod
    def rollback(cls,folder):
        """Restore verified older code as a new, strictly increasing generation."""
        folder=Path(folder).resolve()
        with deployment_lock(folder=folder.parent,operation='rollback release'):
            previous=folder/'previous'
            target=previous.resolve(strict=True)
            if target.parent!=folder.resolve() or not previous.is_symlink():
                raise ValueError('Previous release must be a local published artifact')
            release=cls.published(target,lambda:False,lambda *args:None)
            cls.published(folder/'desired',lambda:False,lambda *args:None)
            release._publish(folder,rollback=True)
            return release

    def owner_status(self,host):
        """Verify the enrolled node through its API; deployment never falls back to SSH."""
        from agent_client import AgentClient,CONFIG
        settings=json.loads(CONFIG.read_text())
        if host['host'] not in settings['peers']:raise ValueError('Use an enrolled node name')
        from scripts.launcher_discovery import resolve_endpoint
        endpoint=host.get('agent_endpoint') or resolve_endpoint(host['host'])[0]
        client=AgentClient({**host,'agent_endpoint':endpoint})
        try:status=client.call('status',timeout=10)
        finally:client.close()
        if status.get('node')!=host['host'] or not isinstance(status.get('machine'),str) or len(bytes.fromhex(status['machine']))!=32:
            raise ValueError('Update API identity differs from enrolled node')
        if host.get('machine') and host['machine']!=status['machine']:raise ValueError('Update machine identity changed')
        return status

    @contextmanager
    def _deployment_guard(self):
        folder=getattr(self,'publication_folder',None)
        if folder is None:
            # Source builders are used by isolated checks. Operational callers
            # load their immutable publication through desired()/published().
            yield
            return
        with deployment_lock(folder=folder.parent,operation='node rollout'):
            check_publication(folder,self.publication_artifact)
            yield

    @contextmanager
    def parallel_updates(self):
        """Lend workers a scoped guard while the caller owns the fleet lock."""
        import copy
        with self._deployment_guard():
            active=threading.Event();active.set()
            def child():
                instance=copy.copy(self)
                @contextmanager
                def guard():
                    if not active.is_set():raise RuntimeError('Parallel deployment scope ended')
                    folder=getattr(self,'publication_folder',None)
                    if folder is not None:check_publication(folder,self.publication_artifact)
                    yield
                instance._deployment_guard=guard
                return instance
            try:yield child
            finally:active.clear()

    def stage(self,host,status):
        with self._deployment_guard():return self._stage(host,status)

    def _stage(self,host,status):
        if self.stopped():raise InterruptedError('Launcher update stopped')
        require_git_source(self.git_source)
        from agent_client import CONFIG
        public_key=json.loads(CONFIG.read_text())['controller_public_key']
        if len(bytes.fromhex(public_key))!=32:raise ValueError('Invalid pinned controller key')
        payload={'supervisor_source':self.supervisor_source,'supervisor_sha256':hashlib.sha256(self.supervisor_source.encode()).hexdigest(),'machine':status['machine'],'node':status['node'],'revision':self.revision,'files':self.files,'controller_public_key':public_key,'peers':json.loads(CONFIG.read_text())['peers']}
        payload['git_source']=self.git_source
        from scripts.node_source_delivery import deliver
        deliver(self,host,status)
        payload['supervisor_supports_source']=status.get('update',{}).get('source_runtime')=='mounted-v1'
        command=self.command(host,self.git_source['activation'],status=status)
        try:result=subprocess.run(command,input=json.dumps(payload),capture_output=True,text=True,timeout=600)
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
        from scripts.launcher_discovery import resolve_endpoint
        owner={**host,'local':False,'machine':status['machine'],
               'agent_endpoint':host.get('agent_endpoint') or resolve_endpoint(name)[0]}
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
        # Ready-node observations cannot stage code. Keep job admission and
        # monitoring available while a different process owns the fleet lock.
        if status.get('ready') and (status.get('source_revision')==self.revision or allow_compatible):
            return self._ensure(host,client,status,allow_compatible=allow_compatible,drain=drain,wait_timeout=wait_timeout)
        with self._deployment_guard():
            return self._ensure(host,client,status,allow_compatible=allow_compatible,drain=drain,wait_timeout=wait_timeout)

    def _ensure(self,host,client,status,allow_compatible=False,drain=False,wait_timeout=240):
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
