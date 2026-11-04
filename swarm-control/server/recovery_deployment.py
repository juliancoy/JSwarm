"""Scoped run.py deployment: drain test runs, preserve API sessions and DNA workers."""
import fcntl
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time
from urllib.request import urlopen

FILES=('task_events.py','task_event_collector.py','retry_jobs.py','granular_dispatch.py','submission_queue.py')
SERVICES=('swarm-control.service','swarm-tasks.service')


def snapshot(url):
    with urlopen(url,timeout=5) as response:return json.load(response)


def stage(source, old, releases):
    files={name:(source/name).read_bytes() for name in FILES}
    for name,value in files.items():compile(value,name,'exec')
    digest=hashlib.sha256(str(old).encode()+b''.join(files.values())).hexdigest()
    target=releases/('task-recovery-'+digest[:20])
    if not target.exists():
        with tempfile.TemporaryDirectory(dir=releases,prefix='.recovery-') as temporary:
            root=Path(temporary)/'release'
            shutil.copytree(old.parent.parent,root,symlinks=True,
                            ignore=shutil.ignore_patterns('.runtime','__pycache__','*.pyc'))
            for name,value in files.items():(root/'swarm-control/server'/name).write_bytes(value)
            (root/'recovery-deployment.json').write_text(json.dumps({'previous':str(old),
                'files':{name:hashlib.sha256(value).hexdigest() for name,value in files.items()}}))
            root.rename(target)
    for name,value in files.items():
        if (target/'swarm-control/server'/name).read_bytes()!=value:
            raise RuntimeError('Recovery release verification failed')
    return target/'swarm-control/server'


def deploy(source):
    home=Path.home();state=home/'.local/state/swarm-control'
    units=home/'.config/systemd/user';releases=home/'.local/share/swarm-control/releases'
    with (state/'deploy.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        originals={name:(units/name).read_text() for name in SERVICES}
        replacements={}
        for name,text in originals.items():
            match=re.search(r'^WorkingDirectory=(.+)$',text,re.M)
            if not match:raise RuntimeError('Missing service source: '+name)
            old=Path(match[1]).resolve()
            if not old.is_relative_to(releases.resolve()) or old.parts[-2:]!=('swarm-control','server'):
                raise RuntimeError('Service does not use an owned immutable release: '+name)
            replacements[name]=text.replace(str(old),str(stage(Path(source),old,releases)))
        drain=state/'distributed/DRAIN';owned=False;changed=False
        try:
            try:
                with drain.open('x'):pass
                owned=True
            except FileExistsError:pass
            deadline=time.monotonic()+900
            while True:
                value=snapshot('http://127.0.0.1:8089/api/distributed-tests')
                if not any(value.get(k) for k in ('running','active_runs','active_reruns')):break
                if time.monotonic()>deadline:raise RuntimeError('Tests still running; recovery deployment deferred')
                print('Waiting for accepted application tests; DNA and conversation services continue.',flush=True)
                time.sleep(5)
            for name,text in replacements.items():
                temporary=(units/name).with_suffix('.recovery.tmp');temporary.write_text(text);temporary.replace(units/name)
                changed=True
            subprocess.run(['systemctl','--user','daemon-reload'],check=True)
            # The conversation API, remote sessions, launchers and DNA services
            # are deliberately outside this deployment's service set.
            subprocess.run(['systemctl','--user','restart',*SERVICES],check=True)
            for attempt in range(20):
                try:
                    snapshot('http://127.0.0.1:8089/api/distributed-tests')
                    snapshot('http://127.0.0.1:8792/api/tasks')
                    break
                except OSError:
                    if attempt==19:raise
                    time.sleep(1)
            print('Recovery rules deployed; API sessions and DNA workers preserved.',flush=True)
        except Exception:
            if changed:
                for name,text in originals.items():
                    temporary=(units/name).with_suffix('.rollback.tmp');temporary.write_text(text);temporary.replace(units/name)
                subprocess.run(['systemctl','--user','daemon-reload'],check=True)
                subprocess.run(['systemctl','--user','restart',*SERVICES],check=True)
            raise
        finally:
            if owned:drain.unlink(missing_ok=True)
