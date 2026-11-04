"""Resolve repository-relative code portions to application-owned test jobs."""
from pathlib import PurePosixPath
from work_queue import FILES

BASES = set(FILES) | {'visual', 'p2p', 'mobile','swarm-web','gpu','lifetech'}


def normalize(paths=None, suites=None):
    paths, suites = paths or [], suites or []
    for values in (paths, suites):
        if not isinstance(values, list) or len(values) > 100 or any(not isinstance(v, str) or not v or len(v) > 300 for v in values):
            raise ValueError('Supply at most 100 paths or suite names')
    if not paths and not suites:
        raise ValueError('Select at least one code path or suite')
    if set(suites) - BASES:
        raise ValueError('Unknown suite: ' + ', '.join(sorted(set(suites) - BASES)))
    clean = []
    for value in paths:
        path = PurePosixPath(value)
        if path.is_absolute() or '..' in path.parts or '\\' in value or value.startswith('-'):
            raise ValueError('Use repository-relative paths without traversal')
        clean.append(str(path).rstrip('/'))
    return {'paths': sorted(set(clean)), 'suites': sorted(set(suites))}


def select(tasks, specs, selection):
    if not selection:
        return tasks
    if 'task_ids' in selection:
        # Internal pinned retries identify logical jobs exactly, including build
        # checks. Public request normalization never accepts this field.
        ids=selection['task_ids']
        if not isinstance(ids,list) or not 1<=len(ids)<=64 or any(not isinstance(task,str) for task in ids) or set(ids)-set(tasks):raise ValueError('Pinned retry task is absent from the published catalog')
        return [task for task in tasks if task in set(ids)]
    chosen = {task for task in tasks if specs[task].get('base', task) in selection['suites']}
    for path in selection['paths']:
        exact = []
        for task in tasks:
            spec = specs[task]
            base = spec.get('base', task)
            if base not in FILES or not spec.get('file'):
                continue
            repo, prefix, _ = FILES[base]
            full = str(PurePosixPath(repo) / prefix / spec['file'])
            if full == path or full.startswith(path + '/'):
                exact.append(task)
        if exact:
            chosen.update(exact)
            if PurePosixPath(path).suffix or any(part in ('test','tests') for part in PurePosixPath(path).parts):
                continue
        # Source edits conservatively select the owning component, not guessed
        # individual dependencies. Explicit test paths select only those tests.
        bases = set()
        if path=='swarm-control' or path.startswith('swarm-control/'):
            bases.add('swarm-web')
        for base, (repo, prefix, _) in FILES.items():
            component = str(PurePosixPath(repo) / prefix)
            if component != '.' and (path == component or path.startswith(component + '/') or component.startswith(path + '/')):
                bases.add(base)
        if path == '.':
            chosen.update(tasks)
            continue
        if not bases and (path.startswith('scripts/') or path.startswith('tests/') or '/' not in path):
            bases.add('workspace')
        if not bases:
            raise ValueError('No test component matches ' + path)
        chosen.update(task for task in tasks if specs[task].get('base', task) in bases)
    if not chosen and selection['suites'] in (['p2p'], ['mobile'], ['gpu']) and not selection['paths']:
        return []
    if not chosen:
        raise ValueError('Selection matched no tracked test jobs')
    return [task for task in tasks if task in chosen]
