"""Publish the read-only pipeline view without restarting compute workers."""
import json
from pathlib import Path
import shutil
import subprocess
import tempfile

ROOT=(Path(__file__).resolve().parents[2] if Path(__file__).resolve().parents[1].name in ('GenomeHarbor', 'JSwarm') else Path(__file__).resolve().parents[1])
ASSETS=('pipeline.html','pipeline.css','pipeline.js','pipeline-model.js','pipeline-colors.js','levels-model.js','tasks.html')


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
    for name in ASSETS:atomic_copy(source/'view'/name,view/name)
    atomic_copy(source/'runtime_estimates.py',server/'runtime_estimates.py')
    atomic_copy(source/'task_pipeline.py',server/'task_pipeline.py')
    atomic_copy(source/'task_machines.py',server/'task_machines.py')
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
