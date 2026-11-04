#!/usr/bin/env python3
"""Run a fixed GitHub commit test shard in disposable Docker containers."""
from paths import WORKSPACE
import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import threading
import time

PREPARATION_PROTOCOL = 1
NODE = 'node:22-bookworm'
BROWSER = 'mcr.microsoft.com/playwright:v1.59.1-noble'
PYTHON = 'python:3.12-bookworm'
# Commands are application-owned, never accepted from dashboard requests.
SUITES = {
    'gpu': {'name':'CUDA container kernel validation','coordinated':True,'image':PYTHON},
    'mobile': {'name':'Pixel 3 · real Android browser','coordinated':True,'image':NODE},
    'p2p': {'name': 'Cross-device WebRTC P2P', 'coordinated': True, 'image': 'selenium/standalone-chrome:latest'},
    'monitor': {'name': 'LAN monitor Python', 'cwd': 'swarm-control/server', 'image': PYTHON,
                'prepare': ['python', '-m', 'pip', 'install', '--target', '/work/.test-deps/monitor', '-r', 'requirements-codex.txt', '-r', 'requirements-p2p.txt', 'pytest'],
                'test': ['python', '-m', 'pytest', 'tests', '-q', '--tb=short']},
    'map': {'name': 'LAN map model', 'cwd': 'swarm-control/server', 'image': NODE,
            'test': ['npm', 'test']},
    'swarm-web': {'name':'Swarm Control browser','cwd':'swarm-control','image':BROWSER,
                  'prepare':['npm','ci','--no-audit','--no-fund'],
                  'test':['node','server/scripts/review-codex-ui.mjs','/work/swarm-web-artifacts']},
    'workspace': {'name': 'Workspace tooling', 'cwd': '.', 'image': 'orgportal-distributed-python-node:1',
                  'prepare': ['python', '-m', 'pip', 'install', '--target', '/work/.test-deps/workspace', '-r', 'requirements.txt', 'pytest'],
                  'test': ['python', '-m', 'pytest', 'tests', '-q', '--tb=short']},
    'usajobs': {'name': 'USAJOBS responsive browser', 'cwd': 'CodeCollective', 'image': BROWSER,
                'prepare': ['npm', 'install', '--prefix', '/work/.test-deps/usajobs', '--no-save',
                            '--package-lock=false', '--ignore-scripts', 'playwright@1.59.1'],
                'test': ['node', '--test', 'tests/usajobs-mobile.test.cjs']},
    'lifetech': {'name': 'LifeTech and MedTech homepage', 'cwd': 'BmoreMedTech', 'image': BROWSER,
                 'prepare': ['npm', 'ci', '--no-audit', '--no-fund'],
                 'test': ['sh', '-c', 'export CHROME_BIN=$(find /ms-playwright -path "*/chrome-linux*/chrome" -type f | head -n 1); node --test scripts/*.test.mjs && npm run build:lifetech && npx vite build']},
    'org-api': {'name': 'Organization API', 'cwd': 'OrgPortal/org-worker', 'image': NODE,
                'prepare': ['sh', '-c', 'npm ci --prefix ../chat-worker --omit=dev --no-audit --no-fund && npm ci --no-audit --no-fund'],
                'test': ['sh', '-c', 'npm test && npm run typecheck && npm run check:bundle']},
    'chat': {'name': 'Chat API', 'cwd': 'OrgPortal/chat-worker', 'image': NODE,
             'prepare': ['npm', 'ci', '--no-audit', '--no-fund'],
             'test': ['sh', '-c', 'npm test && npm run typecheck']},
    'web': {'name': 'Portal frontend', 'cwd': 'OrgPortal/web', 'image': NODE,
            'prepare': ['sh', '-c', 'npm ci --prefix ../org-worker --omit=dev --no-audit --no-fund && npm ci --no-audit --no-fund'],
            'test': ['sh', '-c', 'npm test -- --maxWorkers=2 && npm run build']},
    'visual': {'name': 'Portal layout renders', 'cwd': 'OrgPortal/web', 'image': BROWSER,
               'prepare': ['sh', '-c', 'npm ci --prefix ../org-worker --omit=dev --no-audit --no-fund && npm ci --no-audit --no-fund'],
               'test': ['node', 'scripts/render-layout-fixtures.mjs', '/work/visual-artifacts']},
    'identity': {'name': 'Identity API and browser regression', 'cwd': 'PIdP/serverless', 'image': BROWSER,
                 'prepare': ['npm', 'ci', '--no-audit', '--no-fund'],
                 'test': ['sh', '-c', 'npm run typecheck && node --import tsx --test test/*.test.mjs test/*.test.ts']},
    'identity-python': {'name': 'Identity Python', 'cwd': 'PIdP', 'image': PYTHON,
                        'prepare': ['python', '-m', 'pip', 'install', '--target', '/work/.test-deps/identity-python', '-r', 'requirements-test.txt', 'pytest'],
                        'test': ['python', '-m', 'pytest', 'tests', '-q', '--tb=short']},
}


def emit(kind, **fields):
    with OUTPUT_LOCK:
        print(json.dumps({'kind': kind, **fields}), flush=True)


OUTPUT_LOCK = threading.Lock()


def clean(line):
    line = re.sub(r'\x1b\[[0-9;]*[A-Za-z]', '', line).rstrip()
    line = re.sub(r'(?i)(bearer\s+)[A-Za-z0-9._~-]+', r'\1[redacted]', line)
    line = re.sub(r'(?i)((?:password|secret|api[_-]?key|access[_-]?token)\s*[=:]\s*)[^\s,;]+', r'\1[redacted]', line)
    return line[:2000]


def parse_counts(line, counts):
    fields = {'tests': 'tests', 'pass': 'passed', 'fail': 'failed', 'skipped': 'skipped'}
    match = re.match(r'^(?:#|ℹ)\s+(tests|pass|fail|skipped)\s+(\d+)', line)
    if match:
        counts[fields[match[1]]] = int(match[2])
    for word, key in [('passed', 'passed'), ('failed', 'failed'), ('skipped', 'skipped')]:
        match = re.search(r'(\d+) ' + word + r'\b', line)
        if match and ('Tests ' in line or re.search(r'\d+ (?:passed|failed).* in ', line)):
            counts[key] = int(match[1])
    match = re.match(r'^Ran (\d+) tests? in ', line)
    if match:
        counts['tests'] = int(match[1])
    if line == 'OK' and 'tests' in counts:
        counts['passed'] = counts['tests']; counts['failed'] = 0
    match = re.match(r'^FAILED \((.*)\)', line)
    if match:
        counts['failed'] = sum(int(n) for n in re.findall(r'(?:failures|errors)=(\d+)', match[1]))
        if 'tests' in counts:
            counts['passed'] = max(0, counts['tests'] - counts['failed'])
    if 'tests' not in counts and ('passed' in counts or 'failed' in counts):
        counts['tests'] = sum(counts.get(k, 0) for k in ('passed', 'failed', 'skipped'))
    return counts


def dependency_cache(work,suite_id,spec):
    """Partition reusable package artifacts by install inputs and pinned runtime."""
    digest=hashlib.sha256(('v2\0'+suite_id+'\0'+spec['image']+'\0'+os.uname().machine+'\0'+spec['cwd']+'\0'+json.dumps(spec.get('prepare'),sort_keys=True)).encode())
    cwd=work/spec['cwd']
    names={'package.json','package-lock.json','npm-shrinkwrap.json','yarn.lock','pnpm-lock.yaml',
           'pyproject.toml','poetry.lock','uv.lock','Pipfile.lock'}
    paths=[path for path in cwd.iterdir() if path.is_file() and
           (path.name in names or path.name.startswith('requirements') and path.suffix=='.txt')] if cwd.is_dir() else []
    paths.sort(reverse=True)
    seen=set()
    while paths:
        path=paths.pop();relative=path.resolve().relative_to(work.resolve())
        if relative in seen:continue
        seen.add(relative);content=path.read_bytes();digest.update(str(relative).encode()+b'\0'+content)
        if path.suffix=='.txt':
            for line in content.decode().splitlines():
                match=re.match(r'^\s*(?:-r\s*|-c\s*|--requirement[= ]|--constraint[= ])([^#\s]+)',line)
                if match:paths.append(path.parent/match[1])
    root=Path.home()/'.cache/organization-tests/packages'/digest.hexdigest()
    root.mkdir(parents=True,exist_ok=True,mode=0o700)
    for name in ('npm','pip'):(root/name).mkdir(exist_ok=True,mode=0o700)
    (root/'.last-used').touch()
    return root

def download_cache():
    # npm and pip verify content-addressed downloads themselves. Source/runtime
    # changes invalidate installed trees, not already downloaded package bytes.
    root=Path.home()/'.cache/organization-tests/packages/downloads'
    root.mkdir(parents=True,exist_ok=True,mode=0o700)
    for name in ('npm','pip'):(root/name).mkdir(exist_ok=True,mode=0o700)
    (root/'.last-used').touch()
    return root


def prepare_package(work,task,package,spec,run_id,env,runner,cancel=None):
    """Cache installed trees, but extract a private writable copy for each job."""
    import fcntl
    import shutil
    import tarfile
    import tempfile
    cwd=work/spec['cwd']
    if not cwd.is_dir():return runner(work,task,'prepare',spec,run_id,env,cancel=cancel)
    if package in ('chat','org-api','web','identity'):
        lock=cwd/'package-lock.json'
        if not lock.exists():return runner(work,task,'prepare',spec,run_id,env,cancel=cancel)
        packages=json.loads(lock.read_text()).get('packages',{})
        if any(item.get('link') or str(item.get('resolved','')).startswith('file:') for item in packages.values()):
            return runner(work,task,'prepare',spec,run_id,env,cancel=cancel)
        destination=cwd/'node_modules'
        command=spec['prepare']
        # Granular package installers have one install followed by fresh metadata.
        if command[:2]!=['sh','-c'] or not command[2].startswith('npm ci '):
            return runner(work,task,'prepare',spec,run_id,env,cancel=cancel)
        tail=command[2].split('&&',1)[1].strip() if '&&' in command[2] else None
    elif package in ('monitor','workspace','identity-python','usajobs'):
        destination=work/'.test-deps'/package;tail=None
    else:return runner(work,task,'prepare',spec,run_id,env,cancel=cancel)
    cache=dependency_cache(work,package,spec)
    archive=cache/'installed-v1.tar.gz'
    with (cache.parent/(cache.name+'.lock')).open('a') as lockfile:
        while True:
            try:fcntl.flock(lockfile,fcntl.LOCK_EX|fcntl.LOCK_NB);break
            except BlockingIOError:
                if (work.parent/'STOP').exists() or cancel is not None and cancel.is_set():return 130
                time.sleep(.2)
        if (work.parent/'STOP').exists() or cancel is not None and cancel.is_set():return 130
        if archive.exists() and not destination.exists():
            try:
                with tarfile.open(archive,'r:gz') as tar:
                    members=tar.getmembers()
                    if len(members)>200000 or sum(member.size for member in members)>2*1024**3:raise ValueError('Dependency archive is too large')
                    if any(Path(member.name).parts[0]!=destination.name for member in members):raise ValueError('Invalid dependency archive root')
                    destination.parent.mkdir(parents=True,exist_ok=True)
                    with tempfile.TemporaryDirectory(dir=destination.parent,prefix='.dependency-restore-') as staging:
                        tar.extractall(staging,filter='data')
                        restored=Path(staging)/destination.name
                        if not restored.is_dir() or restored.is_symlink():raise ValueError('Incomplete dependency archive')
                        restored.rename(destination)
            except (OSError,ValueError,tarfile.TarError):
                shutil.rmtree(destination,ignore_errors=True);archive.unlink(missing_ok=True)
            else:
                emit('log',suite=task,phase='prepare',line='Reused installed dependencies for '+package+' in an isolated job copy')
                setup={**spec,'prepare':['sh','-c',tail] if tail else None}
                return runner(work,task,'prepare',setup,run_id,env,cancel=cancel)
        code=runner(work,task,'prepare',spec,run_id,env,cancel=cancel)
        if code==0 and destination.is_dir() and not destination.is_symlink():
            temporary=None
            try:
                with tempfile.NamedTemporaryFile(dir=cache,suffix='.tmp',delete=False) as output:temporary=Path(output.name)
                with tarfile.open(temporary,'w:gz',compresslevel=1) as tar:tar.add(destination,arcname=destination.name)
                temporary.replace(archive)
            except OSError as error:
                emit('log',suite=task,phase='prepare',line='Dependency cache write skipped: '+clean(str(error)))
            finally:
                if temporary is not None:temporary.unlink(missing_ok=True)
        return code


def container_user():
    options = json.loads(subprocess.check_output(
        ['docker', 'info', '--format', '{{json .SecurityOptions}}'], text=True, timeout=25))
    # Rootless container UID 0 maps to the host account that owns the checkout.
    if any(option in ('rootless', 'name=rootless') for option in options):
        return '0:0'
    return str(os.getuid()) + ':' + str(os.getgid())


def run(work, suite_id, phase, spec, run_id, env, cancel=None):
    if spec.get('native') or not spec.get(phase):return _run(work,suite_id,phase,spec,run_id,env,cancel)
    import sys
    sys.path.insert(0,str(WORKSPACE))
    from scripts.launcher_agent import ResourceLease
    lease=ResourceLease(run_id,int(spec.get('cpus',2)),int(spec.get('memory_mb',3072)),
                        'orgportal-test-'+run_id+'-'+suite_id,directory=env.get('SWARM_RESOURCE_DIR'),pids=int(spec.get('pids_limit',512)))
    try:
        emit('phase',suite=suite_id,phase='waiting_for_capacity')
        deadline=time.monotonic()+7200
        while not lease.try_acquire():
            if (work.parent/'STOP').exists() or (cancel is not None and cancel.is_set()):return 130
            if time.monotonic()>deadline:raise TimeoutError('Resource admission deadline exceeded')
            time.sleep(.1)
        return _run(work,suite_id,phase,spec,run_id,env,cancel)
    finally:lease.close()


def _run(work, suite_id, phase, spec, run_id, env, cancel=None):
    if (work.parent / 'STOP').exists() or (cancel is not None and cancel.is_set()):
        return 130
    command = spec.get(phase)
    if not command:
        return 0
    name = 'orgportal-test-' + run_id + '-' + suite_id
    cwd = work / spec['cwd']
    native = spec.get('native') or (phase == 'test' and spec.get('host_fixture'))
    if phase == 'test' and spec.get('host_fixture'):
        from work_queue import HOST_DOCKER_FIXTURES
        if spec.get('base') != 'workspace' or spec.get('file') not in HOST_DOCKER_FIXTURES:
            raise ValueError('Host fixtures require an explicitly reviewed test file')
        temporary = work / '.host-fixtures' / suite_id
        temporary.mkdir(parents=True, exist_ok=True, mode=0o700)
        home = temporary / 'home'
        home.mkdir(exist_ok=True, mode=0o700)
        env = {**env, 'HOME':str(home), 'TMPDIR':str(temporary),
               'PYTHONPATH':str(work/'.test-deps/workspace')+os.pathsep+str(work)+os.pathsep+str(work/'scripts')}
    if not native:
        cache=download_cache()
        command = ['docker', 'run', '--pull=never', '--rm', '--name', name, '--label','org.lifetech.worker=true','--label','org.lifetech.job='+name,'--label','org.lifetech.group='+run_id, '--user', env.get('ORGANIZATION_CONTAINER_USER', str(os.getuid()) + ':' + str(os.getgid())), '-e', 'HOME=/tmp', '--cpus', str(spec.get('cpus',2)), '--memory', str(spec.get('memory_mb',3072))+'m',
                   '--pids-limit', str(spec.get('pids_limit',512)), '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges',
                   '--shm-size', '256m', '--network', 'host' if phase == 'prepare' else 'none',
                   '--mount', 'type=bind,src=' + str(work) + ',dst=/work',
                   '--mount', 'type=bind,src=' + str(cache) + ',dst=/cache',
                   '-e','npm_config_cache=/cache/npm','-e','PIP_CACHE_DIR=/cache/pip',
                   '-e','PIP_DISABLE_PIP_VERSION_CHECK=1', '-w', '/work/' + spec['cwd'],
                   '-e', 'CI=true', '-e', 'PYTHONPATH=/work/.test-deps/' + spec.get('base',suite_id) + ':/work:/work/scripts', '-e', 'SECRET_KEY=distributed-tests-only', '-e', 'DATABASE_URL=postgresql+asyncpg://test:test@127.0.0.1/test',
                   '-e', 'PIDP_BUILD_COMMIT=' + env['PIDP_BUILD_COMMIT'], '-e', 'PIDP_BUILD_DIRTY=false',
                   '-e', 'ORGPORTAL_BUILD_COMMIT=' + env['ORGPORTAL_BUILD_COMMIT'], '-e', 'ORGPORTAL_BUILD_DIRTY=false',
                   spec['image'], *command]
    if (work.parent / 'STOP').exists() or (cancel is not None and cancel.is_set()):
        return 130
    emit('phase', suite=suite_id, phase=phase)
    timeout = 900 if phase == 'prepare' else 1200
    process = None
    counts = {}
    failure_context = 0
    failure_lines = 0
    try:
        process = subprocess.Popen(command, cwd=cwd, env=env, stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT, text=True, errors='replace', start_new_session=True)
        expired = threading.Event()
        def expire():
            expired.set()
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
        def watch_stop():
            while process.poll() is None:
                if (work.parent / 'STOP').exists() or (cancel is not None and cancel.is_set()):
                    # docker run forwards SIGTERM to container PID 1, which may
                    # ignore it. Remove the owned container before joining setup.
                    if not native:
                        try:
                            subprocess.run(['docker', 'rm', '-f', name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20)
                        finally:
                            if process.poll() is None:os.killpg(process.pid, signal.SIGKILL)
                    elif process.poll() is None:os.killpg(process.pid, signal.SIGTERM)
                    return
                time.sleep(0.25)
        threading.Thread(target=watch_stop, daemon=True).start()
        timer = threading.Timer(timeout, expire)
        timer.start()
        try:
            for line in process.stdout:
                text = clean(line)
                emit('log', suite=suite_id, phase=phase, line=text)
                if re.search(r'^(?:not ok |FAIL(?:ED)?|ERROR[: ])|(?:Error|AssertionError):', text):
                    failure_context = 30
                if failure_context and failure_lines < 240:
                    emit('failure', suite=suite_id, line=text)
                    failure_context -= 1
                    failure_lines += 1
                previous = dict(counts)
                parse_counts(text, counts)
                if counts != previous and phase == 'test':
                    emit('counts', suite=suite_id, counts=counts)
            code = process.wait(timeout=10)
        finally:
            timer.cancel()
        return 130 if cancel is not None and cancel.is_set() else 124 if expired.is_set() else code
    except Exception as exc:
        emit('log', suite=suite_id, phase=phase, line=type(exc).__name__ + ': ' + clean(str(exc)))
        return 125
    finally:
        if process and process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
        if process and process.stdout:process.stdout.close()
        if not native:
            subprocess.run(['docker', 'rm', '-f', name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('index', type=int)
    args = parser.parse_args()
    work = WORKSPACE
    manifest = json.loads((work / 'distributed-plan.json').read_text())
    target = manifest['hosts'][args.index]
    if manifest.get('granular') or any('--' in task for task in target['suites']):
        from work_queue import catalog
        catalog(work, SUITES, manifest['repositories'],
                bases={task.split('--',1)[0] for task in manifest.get('task_catalog',target['suites'])})
    # No inherited deployment, cloud, database, or SSH credentials enter a job.
    env = {key: value for key, value in os.environ.items() if key in ('PATH', 'LANG', 'LC_ALL', 'HOME', 'DOCKER_HOST')}
    env['ORGANIZATION_CONTAINER_USER'] = container_user()
    env.update(CI='true', PYTHONDONTWRITEBYTECODE='1',
               PIDP_BUILD_COMMIT=manifest['repositories']['PIdP']['commit'],
               ORGPORTAL_BUILD_COMMIT=manifest['repositories']['OrgPortal']['commit'])
    emit('host', hostname=__import__('socket').gethostname(), cpus=os.cpu_count(),
         python=__import__('sys').version.split()[0], suites=target['suites'])
    for image in dict.fromkeys([PYTHON]+[SUITES[suite]['image'] for suite in target['suites'] if manifest.get('granular') or target['jobs'][suite]['status']!='reused']):
        result = subprocess.run(['docker', 'image', 'inspect', image, '--format', '{{json .Id}}'],
                                capture_output=True, text=True, timeout=25)
        if result.returncode or json.loads(result.stdout) != target['images'].get(image):
            emit('log', phase='images', line='Required image was not distributed or its ID differs: ' + image)
            raise SystemExit(125)
    # Bind sources belong to this Docker daemon's host, not the coordinator.
    # Probe the exact GitHub checkout manifest before allowing any suite to execute.
    snapshot=work/'distributed-plan.json'
    expected=hashlib.sha256(snapshot.read_bytes()).hexdigest()
    probe=['docker','run','--pull=never','--rm','--network','none',
           '--user',env['ORGANIZATION_CONTAINER_USER'],'--cap-drop','ALL',
           '--memory','128m','--pids-limit','32',
           '--mount','type=bind,src='+str(work)+',dst=/work,readonly',
           target['images'][PYTHON],'python3','-c',
           "import hashlib,pathlib;print(hashlib.sha256(pathlib.Path('/work/distributed-plan.json').read_bytes()).hexdigest())"]
    # Cold daemon startup can queue behind other admitted container work.
    result=subprocess.run(probe,capture_output=True,text=True,timeout=180,env=env)
    if result.returncode or result.stdout.strip()!=expected:
        emit('log',phase='mount',line='Docker cannot read this host snapshot. Bind mounts must reference files on the daemon host; use a local Docker context on each agent worker.')
        raise SystemExit(125)
    emit('log',phase='mount',line='Verified host-local source bind mount against the snapshot SHA-256')
    if manifest.get('prepare_only'):
        from granular_worker import prepare_snapshot
        prepare_snapshot(work,manifest,target,env)
        return
    if manifest.get('granular') or any('--' in task for task in target['suites']):
        from granular_worker import execute
        execute(work,manifest,target,env,SUITES)
        return
    for suite_id in target['suites']:
        if (work.parent / 'STOP').exists():
            break
        if target.get('jobs',{}).get(suite_id,{}).get('status')=='reused':
            continue
        spec = SUITES[suite_id]
        if spec.get('coordinated'):
            continue  # The coordinator must drive real peers together, across hosts.
        # Execute the verified image ID, independent of later tag changes.
        spec = {**spec, 'image': target['images'][spec['image']]}
        started = time.monotonic()
        emit('suite', suite=suite_id, status='running', name=spec['name'])
        status = run(work, suite_id, 'prepare', spec, manifest['id'], env)
        if status == 0:
            status = run(work, suite_id, 'test', spec, manifest['id'], env)
        if suite_id in ('visual','swarm-web'):
            try:
                from visual_artifacts import publish
                publish(work / ('visual-artifacts' if suite_id=='visual' else 'swarm-web-artifacts'), suite_id, emit)
            except Exception as exc:
                emit('failure', suite=suite_id, line='Visual artifacts: ' + clean(str(exc)))
                if status == 0: status = 125
        emit('suite', suite=suite_id, status='passed' if status == 0 else 'timed_out' if status == 124 else 'failed',
             exit_code=status, duration_seconds=round(time.monotonic() - started, 3))
    emit('complete', observed=datetime.datetime.now(datetime.timezone.utc).isoformat())


if __name__ == '__main__':
    main()
