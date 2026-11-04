#!/usr/bin/env python3
"""Install and operate the complete Swarm Control application."""
import argparse
import json
import os
import fcntl
import shutil
import tarfile
import tempfile
import time
from urllib.request import urlopen
from pathlib import Path
import subprocess
import sys
import venv

_DEPLOY_LOCK=None
_DEPLOY_DRAIN=None

APP = Path(__file__).resolve().parent
ROOT = APP.parent.parent
RUNTIME = Path.home() / '.local/share/swarm-control/venv'
UNITS = Path.home() / '.config/systemd/user'


def run(*args, **kwargs):
    return subprocess.run([str(x) for x in args], check=True, **kwargs)


def main():
    global _DEPLOY_LOCK,_DEPLOY_DRAIN
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['install', 'deploy', 'deploy-recovery', 'start', 'stop', 'restart', 'status', 'test'])
    parser.add_argument('--revision',help='Reviewed Git revision to deploy; defaults to HEAD.')
    args = parser.parse_args()
    if args.action=='deploy-recovery':
        sys.path.insert(0,str(APP/'server'))
        from recovery_deployment import deploy
        return deploy(APP/'server')
    if args.action=='deploy':
        lock_path=Path.home()/'.local/state/swarm-control/deploy.lock'
        lock_path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
        _DEPLOY_LOCK=lock_path.open('a')
        fcntl.flock(_DEPLOY_LOCK,fcntl.LOCK_EX|fcntl.LOCK_NB)
    if args.action in ('install','deploy'):
        source=APP
        state=Path.home()/'.local/state/swarm-control'
        config=Path.home()/'.config/swarm-control'
        if args.action=='deploy':
            sys.path.insert(0,str(APP/'server'))
            import identity
            identity.config('https://compai.lifetech.fyi')
            # Stop admitting new jobs while accepted work finishes. Restart only
            # after all runs drain; deployments never cancel user jobs.
            drain=state/'distributed/DRAIN';drain.parent.mkdir(parents=True,exist_ok=True)
            drain.touch(mode=0o600);_DEPLOY_DRAIN=drain
        if args.action=='deploy':
            revision=subprocess.check_output(['git','-C',str(ROOT),'rev-parse',args.revision or 'HEAD'],text=True).strip()
            release=Path.home()/'.local/share/swarm-control/releases'/revision
            if not release.exists():
                release.parent.mkdir(parents=True,exist_ok=True)
                staging=Path(tempfile.mkdtemp(prefix='.staging-',dir=release.parent))
                try:
                    with tempfile.TemporaryFile() as archive:
                        run('git','-C',ROOT,'archive','--format=tar',revision,'swarm-control',stdout=archive)
                        archive.seek(0)
                        with tarfile.open(fileobj=archive) as tree:tree.extractall(staging,filter='data')
                    staging.rename(release)
                except Exception:
                    shutil.rmtree(staging);raise
            source=release/'swarm-control'
        if not (RUNTIME / 'bin/python').exists():
            venv.EnvBuilder(with_pip=True).create(RUNTIME)
        run(RUNTIME / 'bin/python', '-m', 'pip', 'install', '-r', APP / 'requirements.txt', cwd=ROOT)
        # Resolve locally or over LAN before allowing a registry download.
        whisper_image='ghcr.io/ggml-org/whisper.cpp@sha256:3f86f6bcaa1d83f5c73a578ff263888570eb82688474bf587dcc51e6f3672a1c'
        if subprocess.run(['docker','image','inspect',whisper_image],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL).returncode:
            sys.path.insert(0,str(ROOT))
            from docker_utils import ensure_image
            ensure_image(whisper_image)
        run(sys.executable, source/'server/docker_services.py', 'up', 'whisper')
        # One-time data migration preserves credentials and expiry, without an old API.
        config.mkdir(parents=True,exist_ok=True,mode=0o700)
        integration=Path.home()/'.config/lan-monitor/orgportal.json'
        if integration.is_file() and not (config/'integrations/orgportal.json').exists():
            (config/'integrations').mkdir(exist_ok=True,mode=0o700)
            fd=os.open(config/'integrations/orgportal.json',os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
            with os.fdopen(fd,'wb') as stream:stream.write(integration.read_bytes())
        old=Path.home()/'.config/map-master/codex-devices.json'
        if old.exists() and not (config/'devices.json').exists():
            with old.with_suffix('.lock').open('a+') as lock:
                fcntl.flock(lock,fcntl.LOCK_EX)
                value=json.loads(old.read_text())
                for key,expiry in value['sessions'].items():
                    if isinstance(expiry,(int,float)):
                        value['sessions'][key]={'label':'Previously paired browser','expires':expiry,'created':expiry-30*86400,'last_seen':expiry-30*86400}
                fd=os.open(config/'devices.json',os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
                with os.fdopen(fd,'w') as stream:json.dump(value,stream)
        UNITS.mkdir(parents=True, exist_ok=True)
        if args.action=='deploy':
            try:
                with urlopen('http://127.0.0.1:8089/api/distributed-tests',timeout=3) as response:active=json.load(response)
            except OSError:active={}
            deadline=time.monotonic()+1800
            while active.get('running') or active.get('active_reruns'):
                if time.monotonic()>deadline:raise RuntimeError('Deployment drain timed out; running jobs were preserved')
                print('Waiting for active jobs to drain before deploying Swarm Control…',flush=True)
                time.sleep(5)
                with urlopen('http://127.0.0.1:8089/api/distributed-tests',timeout=5) as response:active=json.load(response)
            run('systemctl','--user','stop','swarm-control.target')
            previous=APP/'server/.runtime'
            if not state.exists() and previous.exists():
                state.parent.mkdir(parents=True,exist_ok=True)
                shutil.move(str(previous),str(state))
        state.mkdir(parents=True,exist_ok=True,mode=0o700)
        for template in (source / 'server/systemd').iterdir():
            text = template.read_text().replace('{{WORKSPACE}}', str(ROOT)).replace('{{SOURCE}}',str(source)).replace('{{STATE}}',str(state)).replace('{{PYTHON}}', str(RUNTIME / 'bin/python')).replace('{{HOME}}', str(Path.home()))
            (UNITS / template.name).write_text(text)
        run('systemctl', '--user', 'daemon-reload')
        run('systemctl', '--user', 'enable', 'swarm-control.target')
        if args.action=='deploy':
            run('systemctl','--user','restart','swarm-control.target')
    elif args.action == 'test':
        run(sys.executable, '-m', 'unittest', 'discover', '-s', 'tests', '-q', cwd=APP / 'server')
        run('npm', 'test', cwd=APP / 'server')
    elif args.action == 'status':
        run('systemctl', '--user', 'status', 'swarm-control.service', 'swarm-control-api.service', 'swarm-control-tunnel.service', '--no-pager')
    else:
        run('systemctl', '--user', args.action, 'swarm-control.target')


if __name__ == '__main__':
    try:main()
    finally:
        if _DEPLOY_DRAIN is not None:_DEPLOY_DRAIN.unlink(missing_ok=True)
        if _DEPLOY_LOCK is not None:_DEPLOY_LOCK.close()
