#!/usr/bin/env python3
"""Deploy only node monitor components; busy worker agents are never restarted."""
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import runpy
import shutil
import subprocess
import sys
import time
import tempfile

ROOT=Path(__file__).resolve().parents[3]
sys.path.insert(0,str(ROOT/'swarm-control/server'))
from agent_updates import LauncherUpdates
from agent_client import AgentClient,CONFIG

INSTALL=r'''import base64,hashlib,json,os,pathlib,subprocess,sys,shlex
os.umask(0o077)
v=json.load(sys.stdin)
if hashlib.sha256(pathlib.Path('/etc/machine-id').read_bytes()).hexdigest()!=v['machine']:raise RuntimeError('Machine identity mismatch')
config=pathlib.Path.home()/'.config/lifetech-launcher/peers.json'
if json.loads(config.read_text())['name']!=v['node']:raise RuntimeError('Enrolled owner mismatch')
raw=base64.b64decode(v['archive'],validate=True)
if hashlib.sha256(raw).hexdigest()!=v['sha256']:raise RuntimeError('Runtime digest mismatch')
root=config.parent/'monitor-releases'/v['sha256'];root.mkdir(parents=True,exist_ok=True)
(root/'runtime.zip').write_bytes(raw)
(root/'run.py').write_text("import sys\nfrom pathlib import Path\nsys.path.insert(0,str(Path(__file__).with_name('runtime.zip')))\nfrom ramtop.swarm import main\nraise SystemExit(main())\n")
unit=pathlib.Path.home()/'.config/systemd/user/swarm-performance.service'
existing=[]
if unit.exists():
 for line in unit.read_text().splitlines():
  if line.startswith('ExecStart='):
   argv=shlex.split(line.split('=',1)[1])
   if argv and pathlib.Path(argv[0]).is_absolute():existing=[pathlib.Path(argv[0])]
candidates=existing+[pathlib.Path.home()/'global/bin/python',pathlib.Path.home()/'.local/share/organization-node/venv/bin/python',pathlib.Path(sys.executable)]
python=None
for candidate in dict.fromkeys(candidates):
 if not candidate.exists():continue
 try:
  if subprocess.run([str(candidate),'-c','import cryptography'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=5).returncode==0:
   python=candidate;break
 except subprocess.TimeoutExpired:continue
if python is None:raise RuntimeError('Enrolled cryptography runtime is unavailable')
unit=pathlib.Path.home()/'.config/systemd/user/swarm-performance.service';unit.parent.mkdir(parents=True,exist_ok=True)
quote=lambda value:json.dumps(str(value).replace('%','%%'))
source='[Unit]\nDescription=Swarm Node — built-in resource telemetry\nAfter=network-online.target\n[Service]\n'
source+='ExecStart='+quote(python)+' '+quote(root/'run.py')+' --config '+quote(config)+'\n'
source+='Environment=SWARM_MONITOR_REVISION='+v['sha256']+'\nRestart=on-failure\nRestartSec=3\nTimeoutStopSec=10\nNoNewPrivileges=true\nUMask=0077\n[Install]\nWantedBy=default.target\n'
changed=not unit.exists() or unit.read_text()!=source
unit.write_text(source)
subprocess.run(['systemctl','--user','daemon-reload'],check=True,stdout=subprocess.DEVNULL)
subprocess.run(['systemctl','--user','enable','--now','swarm-performance.service'],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
if changed:subprocess.run(['systemctl','--user','restart','swarm-performance.service'],check=True,stdout=subprocess.DEVNULL)
print(json.dumps({'node':v['node'],'monitor_revision':v['sha256'],'service':'swarm-performance.service','worker_restart':False}))
'''


def nodes():
    package=ROOT/'ramtop/ramtop'
    archive=runpy.run_path(str(package/'bundle.py'))['encoded_runtime'](package)
    digest=hashlib.sha256(base64.b64decode(archive)).hexdigest()
    config=json.loads(CONFIG.read_text())
    updater=LauncherUpdates(ROOT,lambda:False,lambda *args:None)
    results=[]
    for name,address in config['peers'].items():
        host={'host':name,'local':name==config['name'],'lan_address':address,'agent_endpoint':'http://'+address+':8790'}
        client=AgentClient(host)
        try:
            status=client.call('status',timeout=5)
            payload={'machine':status['machine'],'node':name,'archive':archive,'sha256':digest}
            result=subprocess.run(updater.command(host,INSTALL),input=json.dumps(payload),capture_output=True,text=True,timeout=30)
            if result.returncode:raise RuntimeError('Monitor install failed: '+result.stderr[-300:])
            row=json.loads(result.stdout);row['status']='installed'
        except Exception as error:row={'node':name,'status':'pending','reason':str(error)[:180]}
        finally:client.close()
        results.append(row);print(json.dumps(row),flush=True)
    report={'sampled_at':time.time(),'monitor_revision':digest,'nodes':results}
    folder=Path.home()/'.local/state/swarm-control';folder.mkdir(parents=True,exist_ok=True)
    (folder/'monitor-deployment.json').write_text(json.dumps(report,indent=2))
    return report


def dashboard():
    """Publish an isolated resource API and UI without restarting the job coordinator."""
    unit=Path.home()/'.config/systemd/user/swarm-control.service'
    text=unit.read_text();previous=Path(re.search(r'^WorkingDirectory=(.+)$',text,re.M)[1])
    release=previous.parents[1].parent/('resource-monitor-'+time.strftime('%Y%m%dT%H%M%S'))
    shutil.copytree(previous.parents[1],release,ignore=shutil.ignore_patterns('__pycache__'))
    server=release/'swarm-control/server';source=ROOT/'swarm-control/server'
    for name in ('resource_server.py','status.py','cpu.py','resource_history.py','agent_updates.py','docker_services.py'):
        shutil.copy2(source/name,server/name)
    scripts=release/'scripts';scripts.mkdir(exist_ok=True)
    for name in ('launcher_agent.py','launcher_streams.py','launcher_peers.py','launcher_registry.py','launcher_registration.py','launcher_discovery.py','swarm_node.py'):
        shutil.copy2(ROOT/'scripts'/name,scripts/name)
    destination=release/'ramtop'
    if destination.exists():shutil.rmtree(destination)
    shutil.copytree(ROOT/'ramtop',destination,ignore=shutil.ignore_patterns('__pycache__','*.egg-info','preview.svg'))
    for name in ('monitor.html','monitor.css','monitor.js','monitor-model.js','export-data.js','task-manager.html','task-manager.js','task-manager.css'):
        shutil.copy2(source/'view'/name,server/'view'/name)
    for name in ('index.html','task-manager.html','tests.html'):
        path=server/'view'/name
        if path.exists() and '/monitor.html' not in path.read_text():
            path.write_text(path.read_text().replace('</header>','<a class="monitor-link" href="/monitor.html">Resource monitor</a></header>',1))
    nginx=(server/'nginx.conf').read_text()
    if 'location = /api/resources {' not in nginx:
        nginx=nginx.replace('        location /api/ {','        location = /api/resources {\n            proxy_pass http://127.0.0.1:8795;\n            proxy_read_timeout 10s;\n        }\n        location /api/ {')
    if 'location = /api/v1/swarm/task-manager {' not in nginx:
        nginx=nginx.replace('        location = /api/resources {','        location = /api/v1/swarm/task-manager {\n            proxy_pass http://127.0.0.1:8795;\n            proxy_read_timeout 20s;\n        }\n        location = /api/resources {')
    for endpoint in ('connections','wgs'):
        if 'location = /api/v1/swarm/'+endpoint+' {' not in nginx:
            nginx=nginx.replace('        location = /api/resources {','        location = /api/v1/swarm/'+endpoint+' {\n            proxy_pass http://127.0.0.1:8795;\n            proxy_read_timeout 20s;\n        }\n        location = /api/resources {')
    (server/'nginx.conf').write_text(nginx)
    state=Path.home()/'.local/state/swarm-control';python=Path.home()/'.local/share/swarm-control/venv/bin/python'
    template=(source/'systemd/swarm-control-resources.service').read_text()
    value=template.replace('{{WORKSPACE}}',str(ROOT)).replace('{{STATE}}',str(state)).replace('{{SOURCE}}',str(server.parent)).replace('{{PYTHON}}',str(python))
    (unit.parent/'swarm-control-resources.service').write_text(value)
    collectors=unit.parent/'swarm-control-collectors.service'
    previous_collectors=collectors.read_text() if collectors.exists() else None
    if previous_collectors:collectors.write_text(re.sub(r'^WorkingDirectory=.+$', 'WorkingDirectory='+str(server), previous_collectors, flags=re.M))
    subprocess.run(['systemctl','--user','daemon-reload'],check=True)
    subprocess.run(['systemctl','--user','enable','--now','swarm-control-resources.service'],check=True)
    subprocess.run(['systemctl','--user','restart','swarm-control-resources.service'],check=True)
    # Validate the proposed nginx config before replacing only its stateless view container.
    # Syntax-check in an isolated network namespace using its loopback address.
    # The production listener remains bound to the master's existing LAN address.
    with tempfile.TemporaryDirectory(prefix='swarm-nginx-check-') as temporary:
        check=Path(temporary)/'nginx.conf'
        check.write_text(nginx.replace('listen 10.42.0.1:', 'listen 127.0.0.1:'))
        subprocess.run(['docker','run','--rm','--pull','never','--entrypoint','nginx',
                        '--mount','type=bind,src='+str(check)+',dst=/etc/nginx/nginx.conf,readonly',
                        'nginx:alpine','-t'],check=True)
    subprocess.run(['python3',str(server/'docker_services.py'),'up','view','--recreate'],
                   env={**os.environ,'SWARM_STATE_DIR':str(state),'SWARM_WORKSPACE':str(ROOT)},check=True)
    (state/'monitor-dashboard-deployment.json').write_text(json.dumps({'release':str(release),'coordinator_restarted':False,'previous':str(previous),'previous_collectors_unit':previous_collectors},indent=2))
    print(json.dumps({'dashboard':'http://map.local/monitor.html','release':str(release),'coordinator_restarted':False}),flush=True)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--nodes-only',action='store_true');parser.add_argument('--dashboard-only',action='store_true')
    args=parser.parse_args()
    if not args.dashboard_only:nodes()
    if not args.nodes_only:dashboard()


if __name__=='__main__':main()
