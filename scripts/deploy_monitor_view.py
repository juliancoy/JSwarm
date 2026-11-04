"""Publish monitor assets through run.py; no node or worker restarts."""
import json
from pathlib import Path
import subprocess
from scripts.deploy_pipeline_view import atomic_copy,ROOT

ASSETS=('monitor.html','monitor.js','monitor-model.js','monitor.css','levels-model.js')


def manage(action):
    row=json.loads(subprocess.check_output(['docker','inspect','swarm-control-view'],text=True))[0]
    view=Path(next(m['Source'] for m in row['Mounts'] if m['Destination']=='/usr/share/nginx/html'))
    if action=='status':
        print(json.dumps({'url':'http://jswarm.localhost/monitor.html','deployed':(view/'monitor.html').exists()}));return 0
    for name in ASSETS:atomic_copy(ROOT/'swarm-control/server/view'/name,view/name)
    print('Monitor deployed: http://jswarm.localhost/monitor.html; nodes and compute workers retained')
    return 0
