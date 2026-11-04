"""One pinned checkout per machine; replenish independent file jobs continuously."""
import concurrent.futures
import json
import os
from pathlib import Path
import threading
import time

from distributed_worker import SUITES, emit, run, clean, prepare_package


def check_command(spec,package=None):
    check=spec['check']
    if package is not None and spec.get('base')=='web' and check=='build:bundle':
        scripts=json.loads((Path(package)/'package.json').read_text()).get('scripts',{})
        if check not in scripts and 'build' in scripts:check='build'
    command=['npm','--ignore-scripts','run',check]
    if spec.get('base')=='web' and spec['check']=='build:bundle':
        return ['env','ORGPORTAL_ECOSYSTEM_BUILD_SNAPSHOT=/work/.ecosystem-build-snapshot.json',*command]
    return command


def capacity():
    memory = {}
    for line in Path('/proc/meminfo').read_text().splitlines():
        key, value = line.split(':', 1)
        memory[key] = int(value.split()[0]) // 1024
    # Leave host services room; reserve task memory before accepting work.
    # Keep Docker startup/inspection responsive alongside owner workloads.
    # Per-task leases still account for actual CPU and memory admission.
    available_cpus = len(os.sched_getaffinity(0)) if hasattr(os,'sched_getaffinity') else os.cpu_count() or 1
    return {'cpus': min(4, max(1, available_cpus)),
            'memory_mb': max(0, memory.get('MemAvailable', 0) - 1024)}


class Dependencies:
    def __init__(self, work, manifest, env):
        self.work, self.manifest, self.env = work, manifest, env
        self.guard = threading.Lock()
        self.locks, self.results = {}, {}
        marker=work/'.prepared-packages.json'
        if marker.exists():
            prepared=json.loads(marker.read_text())
            target=next(h for h in manifest['hosts'] if h.get('images'))
            if prepared.get('source_sha256')==manifest.get('source_sha256') and prepared.get('images')==target['images']:
                self.results={name:0 for name in prepared.get('packages',[])}


    def ensure(self, task, spec, timings=None):
        timings = timings if timings is not None else {}
        base = spec.get('base', task)
        # Shared package trees have one installer. Install development dependencies
        # once, avoiding npm ci deleting another file job's running dependencies.
        packages = {
            'org-api': ['chat', 'org-api'], 'chat': ['chat'],
            'web': ['org-api', 'web'], 'visual': ['org-api', 'web'],
            'identity': ['identity'],
        }
        required=list(packages.get(base, [base]))
        if task == 'web--build-bundle':required.append('ecosystem-build')
        for package in required:
            with self.guard:
                lock = self.locks.setdefault(package, threading.Lock())
            emit('phase', suite=task, phase='waiting_for_dependencies')
            waiting=time.monotonic()
            with lock:
                timings['waiting_for_dependencies']=timings.get('waiting_for_dependencies',0)+time.monotonic()-waiting
                if package not in self.results:
                    setup = {**spec, 'cwd': SUITES['web' if package=='ecosystem-build' else package]['cwd']}
                    if package in ('chat', 'org-api', 'web', 'identity'):
                        command = 'npm ci --no-audit --no-fund'
                        if package in ('chat', 'org-api', 'identity'):
                            command += ' && npm run build:metadata'
                        if package == 'web':
                            command += ' && npm run prebuild'
                        setup['prepare'] = ['sh', '-c', command]
                    elif package == 'ecosystem-build':
                        setup['prepare']=['node','--max-old-space-size=2048','scripts/stage-ecosystem-build.mjs','/work/.ecosystem-build-snapshot.json']
                        setup['memory_mb']=3072
                    else:
                        setup['prepare'] = SUITES[package].get('prepare')
                    preparing=time.monotonic()
                    try:self.results[package] = prepare_package(self.work,task,package,setup,self.manifest['id'],self.env,run,cancel=getattr(self,'cancel',None))
                    finally:timings['prepare']=timings.get('prepare',0)+time.monotonic()-preparing
                if self.results[package]:
                    return self.results[package]
        return 0


def execute(work, manifest, target, env, specs):
    SUITES.update(specs)
    inbox = work.parent / 'tasks'
    inbox.mkdir(exist_ok=True, mode=0o700)
    limits = capacity()
    if not manifest.get('granular'):
        for task in target['suites']:
            if target['jobs'][task]['status'] != 'reused':
                if SUITES[task].get('cpus',1) > limits['cpus'] or SUITES[task].get('memory_mb',1536) > limits['memory_mb']:
                    raise RuntimeError('Insufficient CPU/memory for the requested rerun')
                (inbox / task).touch()
        (inbox / 'CLOSED').touch()
    emit('capacity', **limits)
    dependencies = Dependencies(work, manifest, env)
    seen = set()
    active = {}
    allowed = set(manifest.get('task_catalog',target['suites']))
    from worker_preparation import Preparation, execution_capacity
    preparation=Preparation(work,manifest,target,dependencies,limits) if manifest.get('granular') else None
    if preparation:preparation.start()

    def task_run(task):
        started = time.monotonic()
        spec = {**SUITES[task]}
        spec['image'] = target['images'][spec['image']]
        emit('suite', suite=task, name=spec['name'], status='running')
        code = 125
        timings={'waiting_for_dependencies':0.0,'prepare':0.0}
        try:
            code = preparation.results[spec.get('base',task)] if preparation else dependencies.ensure(task, spec, timings)
            if not code and task == 'web--build-bundle':
                code=dependencies.ensure(task,spec,timings)
            if not code:
                if spec.get('base') == 'web' and spec.get('file'):
                    # Vitest CLI paths are substring filters; an exact include
                    # prevents foo.test.ts also collecting foo.test.tsx.
                    config = work / spec['cwd'] / '.distributed-tests' / (task+'.mjs')
                    config.parent.mkdir(exist_ok=True)
                    config.write_text("import original from '../vitest.config.ts';\nexport default {...original,cacheDir:"+
                                      json.dumps('node_modules/.vite-distributed/'+task)+",test:{...original.test,include:"+
                                      json.dumps([spec['file']])+",maxWorkers:1}};\n")
                if spec.get('check'):
                    # Metadata was generated in setup; don't rewrite it under readers.
                    spec['test'] = check_command(spec,work/spec['cwd'])
                testing=time.monotonic()
                try:code = run(work, task, 'test', spec, manifest['id'], env)
                finally:timings['test']=time.monotonic()-testing
            if task in ('visual','swarm-web'):
                from visual_artifacts import publish
                publish(work / ('visual-artifacts' if task=='visual' else 'swarm-web-artifacts'), task, emit)
        except Exception as error:
            emit('failure', suite=task, line=clean(str(error)))
            code = 125
        emit('suite', suite=task, status='stopped' if (work.parent/'STOP').exists() else 'passed' if code == 0 else 'timed_out' if code == 124 else 'failed',
             exit_code=code, duration_seconds=round(time.monotonic() - started, 3),
             phase_seconds={key:round(value,6) for key,value in timings.items()},timing_source='worker_monotonic')

    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=limits['cpus']) as pool:
            while not (work.parent / 'STOP').exists():
                for task, future in list(active.items()):
                    if future.done():
                        future.result()
                        del active[task]
                for path in sorted(inbox.iterdir()):
                    task = path.name
                    if task == 'CLOSED' or task in seen:
                        continue
                    if task not in allowed or task not in SUITES:
                        raise ValueError('Unknown pinned task: ' + task)
                    spec = SUITES[task]
                    if preparation and spec.get('base',task) not in preparation.ready:continue
                    available=execution_capacity(limits,preparing=bool(preparation and preparation.running),p2p=bool(preparation and preparation.p2p()))
                    cpu = sum(SUITES[t].get('cpus', 1) for t in active)
                    memory = sum(SUITES[t].get('memory_mb', 1536) for t in active)
                    if cpu + spec.get('cpus', 1) > available['cpus'] or memory + spec.get('memory_mb', 1536) > available['memory_mb']:
                        continue
                    seen.add(task)
                    active[task] = pool.submit(task_run, task)
                if (inbox / 'CLOSED').exists() and not active and all(p.name in seen or p.name == 'CLOSED' for p in inbox.iterdir()):
                    break
                time.sleep(.1)
    finally:
        if preparation:preparation.close()
    emit('complete')


def prepare_snapshot(work,manifest,target,env):
    """Warm an isolated checkout. This path invokes no test-phase command."""
    dependencies=Dependencies(work,manifest,env)
    for base in target['suites']:
        if (work.parent/'STOP').exists():raise InterruptedError('Preparation stopped')
        spec={**SUITES[base],'base':base,'image':target['images'][SUITES[base]['image']], 'cpus':1}
        code=dependencies.ensure(base,spec)
        if code:raise RuntimeError('Dependency preparation failed: '+base)
    marker={'source_sha256':manifest['source_sha256'],'images':target['images'],
            'packages':[name for name,code in dependencies.results.items() if code==0]}
    (work/'.prepared-packages.json').write_text(json.dumps(marker))
    emit('prepared',source_sha256=manifest['source_sha256'])
