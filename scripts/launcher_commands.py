"""Controller-signed owner command pipes over the enrolled launcher protocol.

No SSH listener or new network port. Bounded, acknowledged byte windows preserve
stdin/stdout through RPC retries. Docker jobs keep their own resource admission.
"""
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import threading
import time

WINDOW=512*1024

RAM_PATH_PROBE=r'''
import json,pathlib,subprocess,sys
swaps=pathlib.Path(sys.argv[2] if len(sys.argv)>2 else '/proc/swaps')
if len(swaps.read_text().splitlines())>1:raise ValueError('RAM-only workers require host swap to be disabled')
for value in json.loads(sys.argv[1]):
 p=pathlib.Path(value).resolve(strict=True)
 kind=subprocess.check_output(['findmnt','-n','-o','FSTYPE','--target',str(p)],text=True).strip()
 if kind not in ('tmpfs','ramfs'):raise ValueError('RAM-only worker mount is not RAM-backed: '+value+' ('+kind+')')
print(json.dumps({'validated':True}))
'''


# The Docker CLI can exit while its exec process keeps filling an abandoned
# pipe. Keep a machine-local process identity so close also stops that group.
OWNER_EXEC = r'''import json,os,pathlib,signal,subprocess,sys
path=pathlib.Path('/tmp')/('swarm-command-'+sys.argv[1]+'.json')
process=subprocess.Popen(json.loads(sys.argv[2]),start_new_session=True)
identity=pathlib.Path('/proc/self/stat').read_text().split()[21]
path.write_text(json.dumps({'wrapper':os.getpid(),'start':identity,'group':process.pid}))
try:raise SystemExit(process.wait())
finally:
 try:os.killpg(process.pid,signal.SIGTERM)
 except ProcessLookupError:pass
 path.unlink(missing_ok=True)
'''
OWNER_CANCEL = r'''import json,os,pathlib,signal,sys
path=pathlib.Path('/tmp')/('swarm-command-'+sys.argv[1]+'.json')
try:
 row=json.loads(path.read_text())
 if pathlib.Path('/proc',str(row['wrapper']),'stat').read_text().split()[21]==row['start']:
  try:os.killpg(row['group'],signal.SIGKILL)
  except ProcessLookupError:pass
except FileNotFoundError:pass
'''

class Command:
    def __init__(self,argv,spawn):
        self.argv=argv;self.lock=threading.Lock();self.changed=threading.Condition(self.lock);self.closed=False;self.touched=time.monotonic()
        self.process=spawn(argv)
        self.input=bytearray();self.input_offset=0;self.last_input=None;self.eof=False
        self.output={name:{'base':0,'data':bytearray(),'eof':False} for name in ('stdout','stderr')}
        self.threads=[threading.Thread(target=self.write_input,daemon=True),*[threading.Thread(target=self.read_output,args=(name,),daemon=True) for name in self.output]]
        for thread in self.threads:thread.start()
    def write_input(self):
        try:
            while True:
                with self.changed:
                    self.changed.wait_for(lambda:self.input or self.eof or self.closed)
                    if self.closed:return
                    if not self.input:return
                    data=bytes(self.input[:65536])
                count=os.write(self.process.stdin.fileno(),data)
                with self.changed:
                    del self.input[:count];self.changed.notify_all()
        except (OSError,ValueError):pass
        finally:
            self.process.stdin.close()
            with self.changed:self.input.clear();self.changed.notify_all()
    def read_output(self,name):
        state=self.output[name];pipe=getattr(self.process,name)
        try:
            while True:
                with self.changed:
                    self.changed.wait_for(lambda:len(state['data'])<WINDOW or self.closed)
                    if self.closed:return
                    credit=WINDOW-len(state['data'])
                data=os.read(pipe.fileno(),min(65536,credit))
                if not data:break
                with self.changed:state['data'].extend(data);self.changed.notify_all()
        except (OSError,ValueError):pass
        finally:
            pipe.close()
            with self.changed:state['eof']=True;self.changed.notify_all()
    def poll(self,args):
        with self.lock:
            self.touched=time.monotonic()
            for name,state in self.output.items():
                ack=args[name]
                if type(ack) is not int or not state['base']<=ack<=state['base']+len(state['data']):raise ValueError('Invalid output acknowledgement')
                del state['data'][:ack-state['base']];state['base']=ack
            raw=base64.b64decode(args.get('data',''),validate=True)
            if len(raw)>WINDOW:raise ValueError('Input window too large')
            offset=args.get('offset',self.input_offset)
            if type(offset) is not int or offset<0:raise ValueError('Invalid input offset')
            signature=(offset,len(raw),hashlib.sha256(raw).hexdigest())
            if raw:
                if offset==self.input_offset:
                    if self.eof:raise ValueError('Input already closed')
                    if len(self.input)+len(raw)>WINDOW:raise ValueError('Input backpressure window exceeded')
                    self.input.extend(raw);self.input_offset+=len(raw);self.last_input=signature
                elif signature!=self.last_input:raise ValueError('Input offset differs')
            if args.get('eof'):
                if offset+len(raw)!=self.input_offset:raise ValueError('EOF input offset differs')
                self.eof=True
            self.changed.notify_all()
            result={'input_offset':self.input_offset,'input_credit':WINDOW-len(self.input)}
            for name,state in self.output.items():
                result[name]=base64.b64encode(state['data']).decode()
                result[name+'_offset']=state['base'];result[name+'_eof']=state['eof']
            result['exit_code']=self.process.poll()
            return result
    def close(self):
        with self.changed:self.closed=True;self.changed.notify_all()
        if hasattr(self.process,'remote_close'):
            self.process.remote_close()
        if self.process.poll() is None:
            self.process.terminate()
            try:self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:self.process.kill();self.process.wait()
        for thread in self.threads:thread.join(timeout=1)


class CommandService:
    def __init__(self,agent=None,spawn=None,idle_seconds=120):
        self.idle_seconds=idle_seconds
        self.agent=agent;self.spawn=spawn or self.owner_spawn;self.rows={};self.starts={};self.closing=set();self.tombstones={};self.lock=threading.Lock();self.helper_lock=threading.Lock()
        self.stopped=threading.Event()
        self.thread=threading.Thread(target=self.reap,daemon=True,name='launcher-command-pipes');self.thread.start()
    def active(self):
        # Exited processes can still have unacknowledged output in their pipes.
        return len(self.starts)+len(self.closing)+len(self.rows)
    def ensure_owner_helper(self,serve_script=None):
        with self.helper_lock:
            if not hasattr(self,'helper'):
                import docker
                descriptor=self.agent.config.parent/'bulk-streams.json'
                home=Path(json.loads(descriptor.read_text())['config_dir']).parent.parent if descriptor.exists() else Path.home()
                if not home.is_absolute() or home in (Path('/'),Path('/tmp')):raise ValueError('Enrolled owner home is unavailable')
                endpoint=os.getenv('DOCKER_HOST','unix:///var/run/docker.sock')
                if not endpoint.startswith('unix:///'):raise ValueError('Owner runtime requires a local Docker socket')
                socket_path=endpoint.removeprefix('unix://')
                with self.agent.client() as client:
                    rootless=any('rootless' in s for s in client.info().get('SecurityOptions',[]))
                    uid=0 if rootless else self.agent.config.parent.stat().st_uid
                    gid=0 if rootless else self.agent.config.parent.stat().st_gid
                    try:image=client.images.get('organization-launcher:webrtc-1').id
                    except docker.errors.ImageNotFound:image=client.images.get('python:3.12-bookworm').id
                    # Device nodes are metadata here: the helper receives no device
                    # grants. Compute workers still acquire their own GPU devices.
                    identity=('owner-service-v1:'+serve_script.name) if serve_script else 'owner-v4:'
                    name='swarm-owner-'+hashlib.sha256((identity+str(home)+endpoint+image).encode()).hexdigest()[:16]
                    config_dir=Path(json.loads(descriptor.read_text())['config_dir']) if descriptor.exists() else self.agent.config.parent
                    command=['python3','-B',str(config_dir/'command-runtimes'/serve_script.name),'--serve',str(config_dir/self.agent.config.name)] if serve_script else ['python3','-c','import time; time.sleep(10**9)']
                    try:container=client.containers.get(name)
                    except docker.errors.NotFound:
                        container=client.containers.create(image,command,name=name,
                            labels={'organization.container-class':'launcher-owner-runtime','org.lifetech.owner-home':str(home)},
                            network_mode='host',user=f'{uid}:{gid}',group_add=[] if rootless else [str(os.stat(socket_path).st_gid)],
                            read_only=True,cap_drop=['ALL'],security_opt=['no-new-privileges'],mem_limit='1024m',memswap_limit='1024m',pids_limit=1024,
                            tmpfs={'/tmp':'rw,nosuid,nodev,size=64m,mode=1777'},working_dir=str(home),
                            environment={'HOME':str(home),'DOCKER_HOST':endpoint,'PYTHONDONTWRITEBYTECODE':'1',**({'JSWARM_OWNER_RUNTIME':'1'} if serve_script else {})},
                            volumes={str(home):{'bind':str(home),'mode':'rw'},socket_path:{'bind':socket_path,'mode':'rw'},'/etc/machine-id':{'bind':'/etc/machine-id','mode':'ro'},
                                     '/dev':{'bind':'/dev','mode':'ro'}},
                            restart_policy={'Name':'unless-stopped'},healthcheck={'test':['NONE']})
                    if (container.labels or {}).get('org.lifetech.owner-home')!=str(home):raise ValueError('Owner runtime identity differs')
                    container.reload()
                    limits=container.attrs.get('HostConfig',{})
                    if 0<limits.get('PidsLimit',0)<1024 or 0<limits.get('Memory',0)<1024**3:
                        container.update(pids_limit=1024,mem_limit='1024m',memswap_limit='1024m')
                    if not container.attrs['State']['Running']:container.start()
                self.helper=name

    def owner_spawn(self,argv):
        if os.getenv('JSWARM_OWNER_RUNTIME')=='1' or not Path('/.dockerenv').exists() or argv[0] in ('docker','nvidia-smi'):
            process=subprocess.Popen(argv,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,start_new_session=True)
            def cancel_group():
                import signal
                if process.poll() is None or not process.stdout.closed or not process.stderr.closed:
                    try:os.killpg(process.pid,signal.SIGTERM)
                    except ProcessLookupError:pass
            process.remote_close=cancel_group
            return process
        self.ensure_owner_helper()
        token=os.urandom(16).hex()
        process=subprocess.Popen(['docker','exec','-i',self.helper,'python3','-c',OWNER_EXEC,token,json.dumps(argv)],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
        def remote_close():
            try:subprocess.run(['docker','exec',self.helper,'python3','-c',OWNER_CANCEL,token],capture_output=True,timeout=10)
            except (OSError,subprocess.SubprocessError):pass
        process.remote_close=remote_close
        return process
    def changed(self):
        if self.agent:self.agent.busy()
    def dispatch(self,op,args,validate=None):
        ident=args.get('id')
        if not isinstance(ident,str) or not re.fullmatch(r'[a-f0-9]{32}',ident):raise ValueError('Invalid command identity')
        if op=='command.storage':
            paths=args.get('paths')
            if set(args)!={'id','paths'} or not isinstance(paths,list) or len(paths)>128 or any(not isinstance(p,str) or not p.startswith('/') or '\0' in p for p in paths):raise ValueError('Invalid worker mount paths')
            process=self.owner_spawn(['python3','-c',RAM_PATH_PROBE,json.dumps(paths)])
            try:out,err=process.communicate(timeout=30)
            except subprocess.TimeoutExpired:
                process.kill();process.communicate();raise
            if process.returncode:raise ValueError(err.decode(errors='replace')[-1000:])
            return json.loads(out)
        if op=='command.start':
            if set(args)!={'id','argv'} or not isinstance(args['argv'],list) or not args['argv'] or any(not isinstance(s,str) or '\0' in s for s in args['argv']) or len(json.dumps(args['argv']))>512*1024:raise ValueError('Invalid owner command arguments')
            while True:
                with self.lock:
                    if ident in self.tombstones:
                        if self.tombstones[ident][0]!=args['argv']:raise ValueError('Command identity already assigned')
                        return {'id':ident,'started':True,'closed':True}
                    row=self.rows.get(ident)
                    if row:
                        if row.argv!=args['argv']:raise ValueError('Command identity already assigned')
                        return {'id':ident,'started':True}
                    event=self.starts.get(ident)
                    if event is None:
                        if Path('/tmp/launcher-agent-draining').exists():raise RuntimeError('Swarm-node is draining for an update; existing command pipes remain available')
                        self.starts[ident]=threading.Event();break
                event.wait()
            self.changed()
            try:
                # Docker startup and process I/O must never hold the registry lock.
                if validate:validate(args['argv'])
                row=Command(args['argv'],self.spawn)
                with self.lock:self.rows[ident]=row
                return {'id':ident,'started':True}
            finally:
                with self.lock:self.starts.pop(ident).set()
                self.changed()
        with self.lock:
            row=self.rows.get(ident)
            if row is None:
                if op=='command.close' and ident in self.tombstones:return {'closed':True}
                raise FileNotFoundError('Command session unavailable; reconnect or retry its work unit')
            if op=='command.close':
                if set(args)!={'id'}:raise ValueError('Invalid command close')
                self.tombstones[ident]=(row.argv,time.monotonic());del self.rows[ident];self.closing.add(ident)
        if op=='command.close':
            try:row.close();return {'closed':True}
            finally:
                with self.lock:self.closing.discard(ident)
                self.changed()
        if op!='command.poll' or set(args)-{'id','stdout','stderr','offset','data','eof'} or not {'stdout','stderr'}<=set(args) or type(args.get('eof',False)) is not bool:raise ValueError('Invalid command polling request')
        return row.poll(args)
    def reap(self):
        while not self.stopped.wait(10):
            with self.lock:
                stale=[]
                for ident,row in tuple(self.rows.items()):
                    if time.monotonic()-row.touched>self.idle_seconds:
                        stale.append(self.rows.pop(ident));self.tombstones[ident]=(row.argv,time.monotonic())
                self.tombstones={k:v for k,v in self.tombstones.items() if time.monotonic()-v[1]<=self.idle_seconds}
            for row in stale:row.close()
            self.changed()
    def close(self):
        self.stopped.set()
        with self.lock:rows=list(self.rows.values());self.rows.clear()
        for row in rows:row.close()


class CommandProxy:
    """Attach the replaceable agent to this node's existing command pipe owner.

    The local Unix socket has no enrollment or network identity. Command IDs,
    subprocesses and acknowledged windows remain in the same process on update.
    """
    def __init__(self,agent,source):
        self.agent=agent;self.config=Path(agent.config);self.source=source
        self.digest=hashlib.sha256(source.encode()).hexdigest()
        self.socket=self.config.parent/('command-runtime-'+self.digest[:16]+'.sock')
        import fcntl
        with (self.config.parent/'command-upgrade.lock').open('a+') as guard:
            fcntl.flock(guard,fcntl.LOCK_EX)
            try:self._call('runtime.status',{});return
            except (OSError,ValueError):pass
            self.start_runtime()

    def sockets(self):
        # Old pipe owners stay reachable across arbitrarily many releases.
        return [self.socket,*sorted(path for path in self.config.parent.glob('command-runtime*.sock') if path!=self.socket)]

    def start_runtime(self):
        source=self.source
        folder=self.config.parent/'command-runtimes';folder.mkdir(mode=0o700,exist_ok=True)
        digest=hashlib.sha256(source.encode()).hexdigest();script=folder/(digest+'.py')
        if not script.exists():
            temporary=folder/(digest+'.tmp');temporary.write_text(source);temporary.chmod(0o600);temporary.replace(script)
        if hashlib.sha256(script.read_bytes()).hexdigest()!=digest:raise ValueError('Command runtime checksum mismatch')
        import sys
        if Path('/.dockerenv').exists() and hasattr(self.agent,'client'):
            # Serve owner processes directly inside their persistent filesystem
            # view. A completed command must not wait on Docker exec attachment.
            helper=CommandService.__new__(CommandService)
            helper.agent=self.agent;helper.helper_lock=threading.Lock()
            helper.ensure_owner_helper(serve_script=script)
        else:
            with (folder/'runtime.log').open('ab') as log:
                subprocess.Popen([sys.executable,'-B',str(script),'--serve',str(self.config)],stdin=subprocess.DEVNULL,stdout=log,stderr=log,start_new_session=True,close_fds=True)
        deadline=time.monotonic()+15
        while time.monotonic()<deadline:
            try:self._call('runtime.status',{});return
            except (OSError,ValueError):time.sleep(.05)
        raise RuntimeError('Persistent command runtime unavailable')

    def _call(self,op,args,path=None):
        import socket
        with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as connection:
            connection.settimeout(35);connection.connect(str(path or self.socket))
            connection.sendall(json.dumps({'op':op,'args':args}).encode()+b'\n')
            with connection.makefile('rb') as stream:
                raw=stream.readline(3*1024*1024+1)
        if not raw.endswith(b'\n') or len(raw)>3*1024*1024:raise ValueError('Command runtime response exceeds limit')
        value=json.loads(raw)
        if not value.get('ok'):
            error=FileNotFoundError if value.get('type')=='FileNotFoundError' else ValueError if value.get('type')=='ValueError' else RuntimeError
            raise error(value.get('error','Command operation failed'))
        return value['result']

    def dispatch(self,op,args,validate=None):
        if not hasattr(self,'routes'):self.routes={}
        if op=='command.storage':return self._call(op,args,self.socket)
        ident=args['id'];cached=self.routes.get(ident)
        if cached is not None:
            try:
                result=self._call(op,args,cached)
                if op=='command.close':self.routes.pop(ident,None)
                return result
            except (FileNotFoundError,ConnectionError):self.routes.pop(ident,None)
        def routed(path):
            result=self._call(op,args,path)
            if op=='command.close':self.routes.pop(ident,None)
            else:
                if len(self.routes)>=4096:self.routes.pop(next(iter(self.routes)))
                self.routes[ident]=path
            return result
        for path in self.sockets():
            try:
                if op=='command.start':
                    try:known=self._call('runtime.lookup',{'id':args['id']},path)['known']
                    except (RuntimeError,ValueError):
                        # Compatibility with the pre-release pipe owner: a poll
                        # cannot start a process or replay input. Invalid output
                        # acknowledgement also proves that this ID exists there.
                        try:self._call('command.poll',{'id':args['id'],'stdout':0,'stderr':0},path);known=True
                        except ValueError:known=True
                        except FileNotFoundError:known=False
                    if known:return routed(path)
                else:return routed(path)
            except (FileNotFoundError,ConnectionError):continue
            except OSError:
                if not path.exists():continue
                raise
        if op=='command.start':
            if validate:validate(args['argv'])
            return routed(self.socket)
        if op=='command.close':return {'closed':True}
        raise FileNotFoundError('Command session unavailable; reconnect or retry its work unit')

    def active(self):
        total=0
        for path in self.sockets():
            try:total+=self._call('runtime.status',{},path)['active']
            except OSError:continue
        return total
    def close(self):pass  # Agent replacement must not close accepted command pipes.


def serve(config):
    import contextlib,fcntl,socketserver
    config=Path(config)
    digest=hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    stem='command-runtime-'+digest[:16]
    lock=(config.parent/(stem+'.lock')).open('a+')
    try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    except BlockingIOError:return
    class Owner:
        def __init__(self):self.config=config
        def busy(self):pass
        def client(self):
            import docker
            return contextlib.closing(docker.from_env(timeout=15))
    service=CommandService(agent=Owner(),idle_seconds=1800)
    class Handler(socketserver.StreamRequestHandler):
        def handle(self):
            self.request.settimeout(35)
            try:
                raw=self.rfile.readline(2*1024*1024+1)
                if not raw.endswith(b'\n') or len(raw)>2*1024*1024:raise ValueError('Command request exceeds limit')
                value=json.loads(raw);op=value['op'];args=value['args']
                if op=='runtime.status':result={'active':service.active(),'pid':os.getpid(),'source_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
                elif op=='runtime.lookup':
                    with service.lock:result={'known':args['id'] in service.rows or args['id'] in service.starts or args['id'] in service.tombstones}
                elif op in ('command.start','command.poll','command.close','command.storage'):result=service.dispatch(op,args)
                else:raise ValueError('Unknown command runtime operation')
                reply={'ok':True,'result':result}
            except Exception as error:reply={'ok':False,'type':type(error).__name__,'error':str(error)[:500]}
            self.wfile.write(json.dumps(reply).encode()+b'\n')
    class Server(socketserver.ThreadingUnixStreamServer):
        daemon_threads=True
        request_queue_size=64
    path=config.parent/(stem+'.sock');path.unlink(missing_ok=True)
    try:
        with Server(str(path),Handler) as server:
            path.chmod(0o600);server.serve_forever(poll_interval=.2)
    finally:service.close();path.unlink(missing_ok=True)


if __name__=='__main__':
    import argparse
    parser=argparse.ArgumentParser();parser.add_argument('--serve',required=True)
    serve(parser.parse_args().serve)
