"""Replace the single node agent from verified releases; preserve Docker workers."""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time


def prepare_update(idle, drain):
    """Close admission for one tick, then replace without waiting for work."""
    if not drain.exists():
        drain.touch()
        # Give admission handlers a full supervisor tick to publish busy state.
        return False
    return True


def startup_timeout(config):
    # HDD nodes can need longer to reconcile thousands of durable work records.
    try:
        value=json.loads(Path(config).read_text()).get('agent_startup_timeout_seconds',180)
        return max(45,min(900,value)) if type(value) in (int,float) and 0<value< float('inf') else 180
    except (OSError,ValueError,TypeError):return 180


def verify_release(folder,releases):
    folder=folder.resolve()
    if folder.parent!=releases.resolve():raise ValueError('Agent release escapes release directory')
    manifest=json.loads((folder/'manifest.json').read_text())
    expected=('launcher_peers.py','launcher_discovery.py','launcher_registration.py','launcher_agent.py','launcher_registry.py')
    if set(manifest)!=set(expected):raise ValueError('Incomplete agent release')
    sources=[]
    for name in expected:
        path=folder/name
        if path.is_symlink():raise ValueError('Agent release contains a mutable source link')
        data=path.read_bytes()
        if hashlib.sha256(data).hexdigest()!=manifest[name]:raise ValueError('Agent release checksum mismatch')
        compile(data,name,'exec');sources.append(data)
    if hashlib.sha256(b''.join(sources)).hexdigest()!=folder.name:raise ValueError('Agent release identity mismatch')
    return folder


class ReleaseCache:
    """Recheck immutable release contents only when their file identity changes."""
    def __init__(self):self.signature=None;self.folder=None

    def verify(self,folder,releases):
        folder=folder.resolve()
        names=('manifest.json','launcher_peers.py','launcher_discovery.py','launcher_registration.py','launcher_agent.py','launcher_registry.py')
        signature=(str(folder),tuple((name,*(getattr((folder/name).lstat(),field) for field in
                   ('st_dev','st_ino','st_size','st_mtime_ns','st_ctime_ns'))) for name in names))
        if signature!=self.signature:
            verified=verify_release(folder,releases)
            self.folder=verified;self.signature=signature
        return self.folder


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--prefix',default='lifetech-')
    args=parser.parse_args()
    base=Path(__file__).resolve().parent
    config=Path(os.environ.get('LIFETECH_PEER_CONFIG',str(Path.home()/'.config/lifetech-launcher/peers.json')))
    singleton=(config.parent/'launcher-supervisor.lock').open('a+')
    if os.geteuid()==0:os.fchown(singleton.fileno(),config.parent.stat().st_uid,config.parent.stat().st_gid)
    try:fcntl.flock(singleton,fcntl.LOCK_EX|fcntl.LOCK_NB)
    except BlockingIOError:raise SystemExit('A launcher supervisor is already running on this machine')
    releases=config.parent/'agent-releases'
    stopped=threading.Event()
    signal.signal(signal.SIGTERM,lambda *_:stopped.set())
    signal.signal(signal.SIGINT,lambda *_:stopped.set())
    child=None;active=None;prior=None;pending=False;started=0;rejected=set();last_failure=None
    marker=Path('/tmp/launcher-agent-ready.json')
    busy=Path('/tmp/launcher-agent-busy.json')
    drain=Path('/tmp/launcher-agent-draining')
    drain.unlink(missing_ok=True)

    def stop_child():
        if child and child.poll() is None:
            child.terminate()
            try:child.wait(timeout=15)
            except subprocess.TimeoutExpired:child.kill();child.wait()

    verifier=ReleaseCache()

    def desired():
        pointer=releases/'current'
        if not pointer.is_symlink():return base
        return verifier.verify(pointer.resolve(),releases)

    def idle():
        if not busy.exists():return True
        value=json.loads(busy.read_text())
        return not value.get('jobs') and not value.get('browsers') and not value.get('transfers')

    def launch(folder):
        if folder!=base:verify_release(folder,releases)
        marker.unlink(missing_ok=True)
        drain.unlink(missing_ok=True)
        return subprocess.Popen([sys.executable,'-B',str(folder/'launcher_peers.py'),'--prefix',args.prefix])

    try:
        while not stopped.is_set():
            try:
                wanted=desired()
                if wanted in rejected:wanted=active or base
                if child is None:
                    active=wanted
                    if active!=base:
                        prior=(releases/'previous').resolve() if (releases/'previous').is_symlink() else base
                        pending=True
                    child=launch(active);started=time.monotonic()
                elif pending:
                    # A delayed legacy update bridge must not re-close startup admission.
                    drain.unlink(missing_ok=True)
                    # Recovering thousands of durable job records can exceed 45s on HDD nodes.
                    ready=json.loads(marker.read_text()) if marker.exists() else {}
                    if child.poll() is None and ready.get('folder')==str(active) and time.time()-ready.get('time',0)<15:
                        pending=False;prior=None
                    elif child.poll() is not None or time.monotonic()-started>startup_timeout(config):
                        last_failure={'release':str(active),'time':time.time(),'reason':'Startup health check failed; restored previous release'}
                        rejected.add(active);stop_child();active=prior;prior=None;pending=False
                        pointer=releases/'current'
                        if active==base:pointer.unlink(missing_ok=True)
                        else:
                            temporary=releases/'rollback.tmp';temporary.unlink(missing_ok=True)
                            temporary.symlink_to(active.name);temporary.replace(pointer)
                        child=launch(active);started=time.monotonic()
                        print('Agent startup failed; restored previous process release',flush=True)
                elif child.poll() is not None:
                    stopped.wait(5);child=launch(active);started=time.monotonic()
                elif wanted!=active:
                    if prepare_update(idle, drain):
                        prior=active;stop_child();active=wanted;pending=True
                        child=launch(active);started=time.monotonic()
                        print('Applying explicit agent release',flush=True)
                else:
                    # Cancelling a staged update must reopen only our own gate.
                    drain.unlink(missing_ok=True)
                status=Path('/tmp/launcher-agent-update.json')
                temporary=status.with_suffix('.tmp')
                temporary.write_text(json.dumps({'active':str(active),'desired':str(wanted),
                                                'phase':'starting' if pending else 'updating' if wanted!=active else 'ready',
                                                'reason':'Declining new work before node replacement; existing Docker jobs continue' if wanted!=active and not pending else '',
                                                'last_failure':last_failure,
                                                'sampled_at':time.time(),'workers_survive_restart':bool(json.loads(busy.read_text()).get('workers_survive_restart')) if busy.exists() else False}))
                temporary.replace(status)
            except (OSError,ValueError,KeyError,TypeError) as error:
                print('Agent update deferred: '+type(error).__name__,flush=True)
            stopped.wait(1)
    finally:
        stop_child();drain.unlink(missing_ok=True)


if __name__=='__main__':main()
