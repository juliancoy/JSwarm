"""Publish the read-only pipeline view without restarting compute workers."""
import json
from pathlib import Path
import shutil
import subprocess
import tempfile

ROOT=(Path(__file__).resolve().parents[2] if Path(__file__).resolve().parents[1].name in ('GenomeHarbor', 'JSwarm') else Path(__file__).resolve().parents[1])
ASSETS=('node-web.js','pipeline.html','pipeline.css','pipeline.js','machine-controls.js','pipeline-model.js','pipeline-colors.js','levels-model.js','tasks.html')


def atomic_copy(source,target):
    with tempfile.NamedTemporaryFile(dir=target.parent,delete=False) as handle:
        temporary=Path(handle.name)
        handle.write(source.read_bytes())
    temporary.chmod(0o644);temporary.replace(target)


def manage(action):
    row=json.loads(subprocess.check_output(['docker','inspect','swarm-control-view'],text=True))[0]
    view=Path(next(m['Source'] for m in row['Mounts'] if m['Destination']=='/usr/share/nginx/html'))
    if action=='status':print(json.dumps({'url':'http://map.local/pipeline.html','deployed':(view/'pipeline.html').exists()}));return 0
    unit=subprocess.check_output(['systemctl','--user','show','swarm-tasks.service','--property=WorkingDirectory','--value'],text=True).strip()
    server=Path(unit)
    if not server.is_dir():raise RuntimeError('Task dashboard source not found')
    source=ROOT/'swarm-control/server'
    assets=('pipeline.js','pipeline.css','machine-controls.js') if action=='device-controls' else ASSETS
    for name in assets:atomic_copy(source/'view'/name,view/name)
    if action!='device-controls':
        atomic_copy(source/'task_registry.py',server/'task_registry.py')
        atomic_copy(source/'task_progress.py',server/'task_progress.py')
        for name in ('tasks.js','tasks.css','tasks-export.js','task-machines.js','task-display.js','export-data.js'):
            atomic_copy(source/'view'/name,view/name)
        atomic_copy(source/'runtime_estimates.py',server/'runtime_estimates.py')
        atomic_copy(source/'task_pipeline.py',server/'task_pipeline.py')
    atomic_copy(source/'task_machines.py',server/'task_machines.py')
    atomic_copy(source/'machine_controls.py',server/'machine_controls.py')
    # Copy only the new POST handler into an existing dashboard; preserve its
    # collector and observation routes across deployments.
    import ast
    dashboard=server/'task_dashboard.py';installed=dashboard.read_text();desired=(source/'task_dashboard.py').read_text()
    cls=next(n for n in ast.parse(desired).body if isinstance(n,ast.ClassDef) and n.name=='Handler')
    method=next(n for n in cls.body if isinstance(n,ast.FunctionDef) and n.name=='do_POST')
    code=''.join(desired.splitlines(keepends=True)[method.lineno-1:method.end_lineno])
    tree=ast.parse(installed);handler=next(n for n in tree.body if isinstance(n,ast.ClassDef) and n.name=='Handler')
    previous=next((n for n in handler.body if isinstance(n,ast.FunctionDef) and n.name=='do_POST'),None)
    lines=installed.splitlines(keepends=True)
    if previous:installed=''.join(lines[:previous.lineno-1])+code+'\n'+''.join(lines[previous.end_lineno:])
    else:installed=''.join(lines[:handler.lineno])+code+'\n'+''.join(lines[handler.lineno:])
    compile(installed,str(dashboard),'exec')
    with tempfile.NamedTemporaryFile(dir=dashboard.parent,delete=False) as handle:
        temporary=Path(handle.name);handle.write(installed.encode())
    temporary.chmod(0o644);temporary.replace(dashboard)
    # Add only the new route, preserving other changes in the active release.
    path=server/'task_dashboard.py';text=path.read_text()
    if "'/api/tasks/pipeline'" not in text:
        text=text.replace("('/api/tasks', '/api/tasks/events')","('/api/tasks', '/api/tasks/events', '/api/tasks/pipeline')")
        marker="            if path.path == '/api/tasks/events':"
        if marker not in text:raise RuntimeError('Dashboard router shape changed; refusing replacement')
        text=text.replace(marker,"            if path.path == '/api/tasks/pipeline':\n                from task_pipeline import snapshot as pipeline_snapshot\n                data=pipeline_snapshot()\n            elif path.path == '/api/tasks/events':")
        compile(text,str(path),'exec')
        with tempfile.NamedTemporaryFile(dir=path.parent,delete=False) as handle:
            temporary=Path(handle.name);handle.write(text.encode())
        temporary.chmod(0o644);temporary.replace(path)
    text=path.read_text()
    if "'/api/tasks/machine'" not in text:
        text=text.replace("'/api/tasks/pipeline')","'/api/tasks/pipeline', '/api/tasks/machine')")
        marker="            if path.path == '/api/tasks/pipeline':"
        if marker not in text:raise RuntimeError('Dashboard pipeline router changed')
        text=text.replace(marker,"            if path.path == '/api/tasks/machine':\n                from task_machines import snapshot as machine_snapshot\n                query=parse_qs(path.query)\n                try:data=machine_snapshot(query.get('host',[''])[0])\n                except ValueError:\n                    self.send_error(404,'Unknown enrolled machine');return\n            elif path.path == '/api/tasks/pipeline':")
        compile(text,str(path),'exec')
        with tempfile.NamedTemporaryFile(dir=path.parent,delete=False) as handle:
            temporary=Path(handle.name);handle.write(text.encode())
        temporary.chmod(0o644);temporary.replace(path)
    subprocess.run(['systemctl','--user','restart','swarm-tasks.service'],check=True)
    print('Pipeline deployed: http://map.local/pipeline.html; compute workers retained')
    return 0
