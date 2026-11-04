"""Pinned-file work catalog and resource-aware, longest-first task selection."""
import hashlib
import json
from pathlib import Path
import statistics
import subprocess
import re

FILES = {
    'monitor': ('.', 'swarm-control/server/', ('tests/**/test*.py',)),
    'map': ('.', 'swarm-control/server/', ('tests/*.test.js',)),
    'workspace': ('.', '', ('tests/**/test*.py',)),
    'usajobs': ('CodeCollective', '', ('tests/usajobs-mobile.test.cjs',)),
    'org-api': ('OrgPortal', 'org-worker/', ('test/*.test.ts',)),
    'chat': ('OrgPortal', 'chat-worker/', ('test/*.test.ts',)),
    'web': ('OrgPortal', 'web/', ('src/**/*.test.ts', 'src/**/*.test.tsx')),
    'identity': ('PIdP', 'serverless/', ('test/*.test.mjs', 'test/*.test.ts')),
    'identity-python': ('PIdP', '', ('tests/**/test_*.py','tests/**/*_test.py')),
}
# These reviewed fixtures start scoped sibling containers through the enrolled host.
HOST_DOCKER_FIXTURES = frozenset({'tests/test_docker_worker_restart.py',
    'tests/test_huref_compare_worker.py','tests/test_huref_followup.py',
    'tests/test_huref_projected.py','tests/test_julian_finalize.py'})

CHECKS = {'org-api': ('typecheck', 'check:bundle'), 'chat': ('typecheck',),
          'web': ('build:bundle', 'typecheck'), 'identity': ('typecheck',)}


def matches(path, pattern):
    expression=re.escape(pattern).replace(r'\*\*/','(?:.*/)?').replace(r'\*','[^/]*')
    return re.fullmatch(expression,path) is not None


def catalog(root, specs, repositories=None, bases=None):
    """Enumerate tracked tests from the published commits, never untracked files."""
    trees = {}
    tasks = []
    for base, (repo, prefix, patterns) in FILES.items():
        if bases is not None and base not in bases:continue
        if repo not in trees:
            revision = repositories[repo]['commit'] if repositories else 'HEAD'
            trees[repo] = subprocess.check_output(
                ['git', '-C', str(Path(root) / repo), 'ls-tree', '-r', '--name-only', '-z', revision],
                timeout=30).decode().split('\0')
        files = sorted(path[len(prefix):] for path in trees[repo] if path.startswith(prefix)
                       and any(matches(path[len(prefix):], p) for p in patterns))
        if not files:
            raise ValueError('No tracked test files found for ' + base)
        for path in files:
            if base in ('monitor', 'workspace', 'identity-python'):
                command = ['python', '-m', 'pytest', path, '-q', '--tb=short']
            elif base == 'web':
                command = ['npm', 'test', '--', path, '--maxWorkers=1']
            else:
                command = ['node', *(['--import', 'tsx'] if base not in ('map','usajobs') else []), '--test', '--test-concurrency=1', path]
            ident = base + '--' + hashlib.sha256(path.encode()).hexdigest()[:16]
            specs[ident] = {**specs[base], 'base': base, 'file': path, 'test': command,
                            'name': specs[base]['name'] + ' · ' + path, 'cpus': 1,
                            'pids_limit':128 if base in ('monitor','workspace') else 256,
                            'memory_mb': 512 if base in ('monitor','map','workspace') else 1536 if base == 'identity' else 1024}
            if base == 'workspace' and path in HOST_DOCKER_FIXTURES:
                specs[ident]['host_fixture'] = True
                specs[ident]['memory_mb'] = 1024
            if base == 'web':
                specs[ident]['test']=['npm','test','--','--config','.distributed-tests/'+ident+'.mjs','--maxWorkers=1']
            tasks.append(ident)
        for check in CHECKS.get(base, ()):
            ident = base + '--' + check.replace(':', '-')
            specs[ident] = {**specs[base], 'base': base, 'check': check,
                            'test': ['npm', 'run', check], 'name': specs[base]['name'] + ' · ' + check,
                            'cpus': 2, 'memory_mb': 1536 if base == 'web' and check == 'typecheck' else 3072}
            tasks.append(ident)
    whole=[base for base,spec in list(specs.items()) if '--' not in base and base not in FILES and not spec.get('coordinated')]
    for base in whole:specs[base].update(base=base,cpus=2,memory_mb=3072 if base=='visual' else 1536)
    return tasks + whole


def pending(folder, tasks):
    samples = {task: [] for task in tasks}
    for path in sorted(Path(folder).glob('*.json'), reverse=True)[:20]:
        try: run = json.loads(path.read_text())
        except (OSError, ValueError): continue
        for host in run.get('hosts', []):
            for task, job in host.get('jobs', {}).items():
                value = job.get('phase_seconds', {}).get('test', job.get('duration_seconds'))
                if task in samples and job.get('status') == 'passed' and isinstance(value, (int, float)) and value > 0:
                    samples[task].append(value)
    counts = {}
    for task in tasks:
        base=task.split('--',1)[0]
        counts[base]=counts.get(base,0)+1
    defaults={'visual':75,'org-api':160,'identity-python':40,'identity':60,'web':30,'monitor':15,'map':10,'workspace':3,'chat':3}
    def estimate(task):
        if samples[task]:return statistics.median(samples[task])
        if task.endswith(('--build', '--build-bundle')):return 45
        if task.endswith('--typecheck'):return 15
        if task.endswith('--check-bundle'):return 10
        base=task.split('--',1)[0]
        return defaults.get(base,30)/counts[base]
    return sorted(tasks, key=estimate, reverse=True)


def claim(queue, specs, capacity, active, ready_bases=None):
    """Reserve both CPU and memory; there is no suite-to-machine ownership."""
    cpu = capacity['cpus'] - sum(specs[t].get('cpus', 1) for t in active)
    memory = capacity['memory_mb'] - sum(specs[t].get('memory_mb', 1536) for t in active)
    for index, task in enumerate(queue):
        spec = specs[task]
        if ready_bases is not None and spec.get('base',task) not in ready_bases:continue
        if spec.get('cpus', 1) <= cpu and spec.get('memory_mb', 1536) <= memory:
            return queue.pop(index)
    return None
