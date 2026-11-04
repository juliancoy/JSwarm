#!/usr/bin/env python3
"""Publish task/resource integration without restarting swarm job coordination."""
import json
import re
import shutil
import subprocess
import time
from pathlib import Path

ROOT=Path(__file__).resolve().parents[4]

def main():
    units=Path.home()/'.config/systemd/user'
    unit=units/'swarm-control-api.service';original=unit.read_text()
    previous=Path(re.search(r'^WorkingDirectory=(.+)$',original,re.M)[1])
    release=previous.parents[1].parent/('task-resources-'+time.strftime('%Y%m%dT%H%M%S'))
    shutil.copytree(previous.parents[1],release,ignore=shutil.ignore_patterns('__pycache__','previous-lan-assets'))
    target=release/'swarm-control/server';source=ROOT/'swarm-control/server'
    for name in ('api.py','status.py'):shutil.copy2(source/name,target/name)
    assets=('task-manager.html','task-manager.js','task-manager.css','monitor.js','monitor.css','monitor-model.js')
    for name in assets:shutil.copy2(source/'view'/name,target/'view'/name)
    # API and LAN frontend keep separate releases; no coordinator or worker restart.
    mount=json.loads(subprocess.check_output(['docker','inspect','swarm-control-view','--format','{{json .Mounts}}'],text=True))
    live=Path(next(row['Source'] for row in mount if row['Destination']=='/usr/share/nginx/html'))
    # Static files are atomic replacements; preserve a rollback copy per deployment.
    backup=release/'previous-lan-assets';backup.mkdir()
    for name in assets:
        path=live/name
        if path.exists():shutil.copy2(path,backup/name)
        temporary=path.with_suffix(path.suffix+'.pending');shutil.copy2(source/'view'/name,temporary);temporary.replace(path)
    resource_unit=(units/'swarm-control-resources.service').read_text()
    resource_root=Path(re.search(r'^WorkingDirectory=(.+)$',resource_unit,re.M)[1])
    for name in ('resource_server.py','status.py'):shutil.copy2(source/name,resource_root/name)
    subprocess.run(['systemctl','--user','restart','swarm-control-resources.service'],check=True)
    config=Path(next(row['Source'] for row in mount if row['Destination']=='/etc/nginx/nginx.conf'))
    nginx=config.read_text()
    original_nginx=nginx
    for endpoint in ('task-manager','connections','wgs'):
        route='        location = /api/v1/swarm/'+endpoint+' {\n            proxy_pass http://127.0.0.1:8795;\n            proxy_read_timeout 20s;\n        }\n'
        if 'location = /api/v1/swarm/'+endpoint+' {' not in nginx:
            nginx=nginx.replace('        location = /api/resources {',route+'        location = /api/resources {')
    if nginx!=original_nginx:
        (release/'previous-nginx.conf').write_text(original_nginx)
        config.write_text(nginx)
        subprocess.run(['docker','exec','swarm-control-view','nginx','-t'],check=True)
        subprocess.run(['docker','exec','swarm-control-view','nginx','-s','reload'],check=True)
    updated=original.replace(str(previous),str(target))
    unit.write_text(updated)
    subprocess.run(['systemctl','--user','daemon-reload'],check=True)
    subprocess.run(['systemctl','--user','restart','swarm-control-api.service'],check=True)
    state=Path.home()/'.local/state/swarm-control';state.mkdir(exist_ok=True,parents=True)
    (state/'task-resources-deployment.json').write_text(json.dumps({'release':str(release),'previous_unit':original,'lan_assets':str(live),'coordinator_restarted':False},indent=2))
    print(json.dumps({'release':str(release),'coordinator_restarted':False,'task_manager':'https://compai.lifetech.fyi/task-manager'}))

if __name__=='__main__':main()
