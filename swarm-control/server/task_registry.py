"""Generic task metadata and progress files shared by independent executors.

Register at start: python task_registry.py --id RUN --title 'Readable task name'
An executor atomically writes <state>/tasks/RUN.json with id, name, status and
children [{id,name,status,worker,completed_units?,total_units?}]. Units are only
used when the executor supplies a known denominator. Registration does not run it.
"""
import argparse
import json
import re
import time
from pathlib import Path
from paths import STATE


def task_view(task):
    children = []
    for row in task.get('children', []):
        child = task_view(row) if row.get('children') else dict(row)
        done = child.get('completed_units'); total = child.get('total_units')
        child['percent'] = child['progress']['percent'] if child.get('children') else (round(100 * min(done, total) / total, 1)
                            if type(done) in (int, float) and type(total) in (int, float) and 0 <= done and 0 < total
                            else 100 if child.get('status') in ('completed', 'passed', 'reused') else None)
        child.setdefault('worker', 'unassigned');children.append(child)
    from task_progress import aggregate
    result = {**task, 'children': children, 'progress': aggregate(children)}
    if children:
        states = [c.get('status') for c in children]
        if all(s in ('completed', 'passed', 'reused', 'verified') for s in states):
            result['status'] = 'partial' if task.get('status')=='partial' else 'completed'
        elif any(s in ('running', 'active', 'preparing', 'transferring', 'starting', 'streaming', 'stopping') for s in states):
            result['status'] = 'running'
        elif any(s in ('failed', 'error', 'conflict', 'timed_out', 'interrupted') for s in states):
            result['status'] = 'failed'
        elif all(s == 'not scheduled' for s in states):
            result['status'] = 'not scheduled'
        elif any(s=='waiting_inputs' for s in states) and all(s in ('completed','passed','reused','verified','waiting_inputs','partial') for s in states):
            result['status'] = 'partial' if task.get('status')=='partial' else 'waiting_inputs'
        elif any(s=='partial' for s in states) and all(s in ('completed','passed','reused','verified','partial') for s in states):
            result['status']='partial'
        elif task.get('status') not in ('failed', 'interrupted', 'partial', 'preparing', 'not scheduled'):
            result['status'] = 'waiting' if any(s in ('waiting', 'blocked', 'waiting_capacity') for s in states) else 'queued'
        # Count leaves so a running workflow can still expose failed work,
        # without counting the same failure again at every parent level.
        result['active_work_items']=sum(c.get('active_work_items',0) if c.get('children') else int(c.get('status') in ('running','active','preparing','transferring','starting','streaming','stopping')) for c in children)
        result['failed_work_items']=sum(c.get('failed_work_items',0) if c.get('children') else int(c.get('status') in ('failed','error','conflict','timed_out','interrupted')) for c in children)
        blockers=[]
        for child in children:
            blockers.extend(child.get('input_blockers',[]))
            if child.get('status')=='waiting_inputs' and not child.get('input_blockers'):
                blockers.append(child.get('detail') or child.get('name') or 'Required evidence is missing')
        if blockers:result['input_blockers']=list(dict.fromkeys(blockers))
        # Bounded sibling work items supply a rough stage estimate only after
        # verified completions. Never extrapolate this over workflow dependencies.
        timed=[c for c in children if c.get('attempt_started') and c.get('elapsed_seconds') is not None]
        finished=[c for c in timed if c.get('status') in ('completed','passed','reused','verified')]
        if (not any(c.get('children') for c in children) and len(finished)>=2
                and result['status']=='running' and not result.get('execution_eta_seconds')):
            elapsed=time.time()-min(c['attempt_started'] for c in timed)
            if elapsed>0:
                result.update(execution_eta_seconds=elapsed*(len(children)-len(finished))/len(finished),
                              eta_scope='Stage', eta_basis='Rough observed completion rate for bounded sibling jobs; assumes current fleet throughput and similar job costs')
    return result


def attach_runtime(task, folder, now=None):
    """Merge fresh operational measurements without transferring execution ownership."""
    now = time.time() if now is None else now
    allowed = ('current_phase', 'completed_units', 'total_units', 'unit', 'units_per_second',
               'sampled_at', 'elapsed_seconds', 'attempt_started', 'execution_eta_seconds',
               'eta_scope', 'eta_basis', 'eta_unavailable_reason', 'basis', 'timing_basis', 'runtime_detail',
               'cpu_seconds', 'cpu_percent', 'rss_bytes', 'process_read_bytes', 'producer_cpu_percent', 'producer_memory')
    def visit(row):
        ident = row.get('id', '')
        if re.fullmatch(r'[A-Za-z0-9_-]{1,100}', ident) and row.get('status') in ('running', 'active', 'transferring', 'streaming'):
            try:
                path = folder / (ident + '.json')
                if path.stat().st_size > 32768: raise ValueError('Runtime sample too large')
                value = json.loads(path.read_text())
                if 0 <= now - value.get('sampled_at', 0) < 60:
                    row.update({k: value[k] for k in allowed if k in value})
                else:
                    row['runtime_detail']='Runtime measurements stale; awaiting a fresh worker sample'
                    row['runtime_stale']=True
            except (OSError, ValueError, TypeError): pass
        for child in row.get('children', []): visit(child)
    visit(task)
    return task


def registry_snapshot(root=STATE):
    tasks = []; plans = []; labels = {}; errors = []
    folder = root/'tasks'
    for path in sorted(folder.glob('*.json')):
        try:
            if path.stat().st_size > 2 * 1024 * 1024: raise ValueError('Task too large')
            value = json.loads(path.read_text())
            if value.get('kind') == 'label': labels[value['id']] = value['name'];continue
            if re.fullmatch(r'[A-Za-z0-9_-]{1,100}', value.get('id', '')):
                attach_runtime(value, root/'task-runtime'/value['id'])
            task = task_view(value)
            indexed={}
            def index(row):
                if row.get('id'): indexed[row['id']]=row
                for child in row.get('children',[]):index(child)
            index(task)
            for row in indexed.values():
                row['waiting_for']=[indexed[dep].get('name',dep) for dep in row.get('depends_on',[])
                                    if dep in indexed and indexed[dep].get('status') not in ('completed','passed','reused','verified')]
            (plans if task.get('status') == 'not scheduled' else tasks).append(task)
        except (OSError, ValueError, KeyError, TypeError): errors.append(path.stem + ': invalid task observation')
    return tasks, plans, labels, errors


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--id', required=True)
    parser.add_argument('--title', required=True)
    parser.add_argument('--subtask', action='append', default=[])
    parser.add_argument('--plan', action='store_true')
    parser.add_argument('--label-existing', action='store_true')
    args = parser.parse_args()
    if not re.fullmatch('[A-Za-z0-9_-]{1,100}', args.id) or not args.title.strip(): parser.error('Valid ID and nonempty title required')
    folder = STATE/'tasks';folder.mkdir(parents=True, exist_ok=True)
    target = folder/(args.id+'.json')
    if target.exists(): parser.error('Task already registered; existing progress was preserved')
    value = {'id':args.id, 'name':args.title, 'status':'not scheduled' if args.plan else 'queued',
             'children':[{'id':str(i),'name':name,'status':'not scheduled' if args.plan else 'queued'} for i,name in enumerate(args.subtask)]}
    if args.label_existing: value['kind']='label'
    from task_events import EventStore
    EventStore().observe(value, 'registry', 'registration')
    with target.open('x') as output: json.dump(value,output)
    print(target)

if __name__ == '__main__': main()
