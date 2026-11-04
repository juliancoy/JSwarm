#!/usr/bin/env python3
"""Deploy the task chart and event journal without stopping worker jobs."""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import time
from urllib.request import urlopen

home=Path.home();workspace=Path(__file__).resolve().parents[4]
work=workspace/'swarm-control/server'
with urlopen('http://127.0.0.1:8089/api/distributed-tests',timeout=5) as response:status=json.load(response)
if status.get('running') or status.get('active_runs') or status.get('active_reruns'):
    raise SystemExit('Coordinator has active tests; workers were preserved. Retry when they finish.')
coordinator_unit=home/'.config/systemd/user/swarm-control.service'
previous_unit=coordinator_unit.read_text()
old_server=Path(re.search(r'^WorkingDirectory=(.+)$',previous_unit,re.M)[1])
old=old_server.parent.parent
release=old.parent/('compute-chart-'+time.strftime('%Y%m%dT%H%M%S'))
shutil.copytree(old,release)
source=release/'swarm-control/server'
files=['docker_services.py','retry_jobs.py','live_distributed.py','input_progress.py','task_progress.py','task_dashboard.py','task_registry.py','task_events.py','task_event_collector.py','distributed.py','granular_dispatch.py']
for name in files:shutil.copy2(work/name,source/name)
for name in ['tasks.html','tasks.js','tasks.css','task-display.js']:shutil.copy2(work/'view'/name,source/'view'/name)
for name in ['tests.html','index.html','genome-explorer.html']:
    p=source/'view'/name
    if p.exists() and '/tasks.html' not in p.read_text():
        p.write_text(p.read_text().replace('</header>','<a class="monitor-link" href="/tasks.html">Outstanding Compute Chart</a></header>',1))
p=source/'nginx.conf';config=p.read_text().replace('location = /api/tasks {','location ^~ /api/tasks {')
if 'location ^~ /api/tasks {' not in config:
    config=config.replace('        location /api/ {','        location ^~ /api/tasks {\n            proxy_pass http://127.0.0.1:8792;\n            proxy_read_timeout 20s;\n        }\n        location /api/ {')
config=config.replace('proxy_pass http://127.0.0.1:8792;\n            proxy_read_timeout 20s;', 'proxy_pass http://127.0.0.1:8792;\n            proxy_buffering off;\n            proxy_http_version 1.1;\n            proxy_read_timeout 60s;')
p.write_text(config)
state=home/'.local/state/swarm-control';python=home/'.local/share/swarm-control/venv/bin/python'
unit=(work/'systemd/swarm-tasks.service').read_text().replace('{{WORKSPACE}}',str(workspace)).replace('{{SOURCE}}',str(source.parent)).replace('{{STATE}}',str(state)).replace('{{PYTHON}}',str(python))
(home/'.config/systemd/user/swarm-tasks.service').write_text(unit)
# Only the idle coordinator restarts; native agents, QC, downloads and other services continue.
coordinator_changed=any(not (old_server/name).exists() or (work/name).read_bytes()!=(old_server/name).read_bytes() for name in ('retry_jobs.py','live_distributed.py','distributed.py','granular_dispatch.py','task_events.py'))
if coordinator_changed:coordinator_unit.write_text(previous_unit.replace(str(old_server),str(source)))
(release/'deployment.json').write_text(json.dumps({'previous_source':str(old_server),'previous_coordinator_unit':previous_unit,'files':{name:hashlib.sha256((source/name).read_bytes()).hexdigest() for name in files}},indent=2))
subprocess.run(['systemctl','--user','daemon-reload'],check=True)
if coordinator_changed:subprocess.run(['systemctl','--user','restart','swarm-control.service'],check=True)
subprocess.run(['systemctl','--user','enable','swarm-tasks.service'],check=True)
subprocess.run(['systemctl','--user','restart','swarm-tasks.service'],check=True)
subprocess.run(['python3',str(source/'docker_services.py'),'up','view','--recreate'],env={**os.environ,'SWARM_STATE_DIR':str(state),'SWARM_WORKSPACE':str(workspace)},check=True)
print('Deployed Outstanding Compute Chart and transactional event journal:',release)
