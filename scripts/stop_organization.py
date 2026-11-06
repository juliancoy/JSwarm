"""Explicit, data-preserving shutdown of the Organization runtime."""
import json
from pathlib import Path
import subprocess
import sys


# Sent over enrolled SSH access so shutdown does not depend on a working agent.
SCRIPT = r'''
import json, os, pathlib, subprocess, time
errors=[]
def run(argv, timeout=120):
    p=subprocess.run(argv,text=True,capture_output=True,timeout=timeout)
    return p
units=run(['systemctl','--user','list-units','--all','--plain','--no-legend','--no-pager']).stdout
owned=[]
for line in units.splitlines():
    fields=line.split()
    if not fields:continue
    name=fields[0]
    if name.endswith(('.service','.timer','.target')) and name.startswith(('lifetech-','swarm-','organization-','venter-','run-input-')):
        owned.append(name)
# Stop producers and timers before containers; stopped units do not auto-restart.
if owned:
    # user.control precedes locally installed user units; ordinary --runtime
    # masks live below ~/.config/systemd/user and cannot override those units.
    masks=pathlib.Path(os.environ.get('XDG_RUNTIME_DIR','/run/user/'+str(os.getuid())))/'systemd/user.control'
    masks.mkdir(parents=True,exist_ok=True)
    for name in owned:
        path=masks/name
        if not path.exists() and not path.is_symlink():path.symlink_to('/dev/null')
        elif not path.is_symlink() or os.readlink(path)!='/dev/null':errors.append('Cannot mask existing '+str(path))
    p=run(['systemctl','--user','daemon-reload'],timeout=30)
    if p.returncode:errors.append(p.stderr.strip())
    p=run(['systemctl','--user','stop',*owned],timeout=240)
    if p.returncode:errors.append(p.stderr.strip())
policy=pathlib.Path.home()/'.config/lifetech-launcher/worker-policy.json'
if policy.parent.exists():
    tmp=policy.with_suffix('.stop.tmp');tmp.write_text(json.dumps({'mode':'paused'}));tmp.chmod(0o600);tmp.replace(policy)
known={'swarm-node','lifetech-launcher','lifetech-launcher-session',
       'medtech-website','lifetech-website','lifetech-website-preview',
       'codecollective-website','codecollective-datacenters-dashboard','codecollective-r8-rowhome',
       'local-browser-automation','local-service-directory','organization-test-registry',
       'local-identity-api','local-identity-db','local-data-replication','portal',
       'common-form-test-identity-db','android-team-form-identity-db'}
stopped=[]
def belongs(v):
    name=v['Name'].lstrip('/'); labels=v['Config'].get('Labels') or {}
    if name in known:return True
    if labels.get('organization.managed-by')=='run.py':return True
    if labels.get('organization.container-class') in ('swarm-node','launcher-owner-runtime'):return True
    if labels.get('org.lifetech.worker')=='true' or labels.get('org.lifetech.service'):return True
    for mount in v.get('Mounts',[]):
        source=mount.get('Source','')
        if '/Documents/Organization/' in source or source.endswith('/Documents/Organization'):return True
    if name.endswith('-identity-db'):
        # Identity databases have only the runner's named PIdP volume, rather
        # than a checkout bind mount. Stopping them retains that volume.
        if any(m.get('Type')=='volume' and m.get('Name','').endswith('PIdP_POSTGRES') for m in v.get('Mounts',[])):return True
    return name.startswith(('lifetech-','bmoremedtech-','swarm-'))
contexts=run(['docker','context','ls','--format','{{.Name}}']).stdout.split()
for context in dict.fromkeys(['default',*(['rootless'] if 'rootless' in contexts else [])]):
    cli=['docker','--context',context]
    p=run(cli+['ps','-q'],timeout=30)
    if p.returncode:
        # An absent daemon is already stopped; report other connection errors.
        if 'Cannot connect' not in p.stderr and 'no such file' not in p.stderr:errors.append(context+': '+p.stderr.strip())
        continue
    ids=p.stdout.split()
    if not ids:continue
    p=run(cli+['inspect',*ids],timeout=30)
    if p.returncode:errors.append(p.stderr.strip());continue
    selected=[v for v in json.loads(p.stdout) if belongs(v)]
    # Launchers first so they cannot recreate application containers.
    selected.sort(key=lambda v: 0 if v['Name'].lstrip('/') in ('swarm-node','lifetech-launcher') or (v['Config'].get('Labels') or {}).get('organization.container-class')=='launcher-owner-runtime' else 1)
    launchers=[v for v in selected if (v['Config'].get('Labels') or {}).get('organization.container-class') in ('swarm-node','launcher-owner-runtime') or v['Name'].lstrip('/') in ('swarm-node','lifetech-launcher','lifetech-launcher-session')]
    for group in (launchers,[v for v in selected if v not in launchers]):
        if not group:continue
        p=run(cli+['stop','--time','20',*[v['Id'] for v in group]],timeout=120)
        if p.returncode:errors.append(p.stderr.strip())
        else:stopped.extend(context+':'+v['Name'].lstrip('/') for v in group)
    p=run(cli+['ps','-q'],timeout=30)
    if p.returncode:errors.append(p.stderr.strip());continue
    if p.stdout.split():
        p=run(cli+['inspect',*p.stdout.split()],timeout=30)
        if p.returncode:errors.append(p.stderr.strip())
        else:
            remaining=[v['Name'] for v in json.loads(p.stdout) if belongs(v)]
            if remaining:errors.append('Still running in '+context+': '+', '.join(remaining))
active=run(['systemctl','--user','list-units','--state=active,activating','--plain','--no-legend','--no-pager']).stdout
remaining=[name for name in owned if any(line.split() and line.split()[0]==name for line in active.splitlines())]
if remaining:errors.append('Still active: '+', '.join(remaining))
print(json.dumps({'host':os.uname().nodename,'stopped_units':owned,'stopped_containers':stopped,'errors':errors}),flush=True)
raise SystemExit(bool(errors))
'''


def stop(root, fleet=False):
    config_path=Path.home()/'.config/lifetech-launcher/peers.json'
    config=json.loads(config_path.read_text()) if fleet else {}
    inventory=Path.home()/'.local/state/orgportal/lan-ssh-devices.json'
    accounts=json.loads(inventory.read_text()).get('accounts',{}) if inventory.exists() else {}
    targets=[(config.get('name','local'),None)]
    if fleet:
        targets += [(host,address) for host,address in config['peers'].items() if host!=config['name']]
    failed=False
    for host,address in targets:
        if address is None:
            command=[sys.executable,'-']
        else:
            command=['ssh','-o','BatchMode=yes','-o','StrictHostKeyChecking=yes','-o','ConnectTimeout=8',
                     '-o','ForwardAgent=no','-o','HostName='+address,'-o','HostKeyAlias='+host]
            key=Path.home()/'.ssh/orgportal_lan_ed25519'
            if key.exists():command+=['-i',str(key)]
            if accounts.get(host):command+=['-l',accounts[host]]
            command += [host,'python3 -']
        print('Stopping Organization on '+host+'…',flush=True)
        try:
            p=subprocess.run(command,input=SCRIPT,text=True,capture_output=True,timeout=600)
            print(p.stdout,flush=True)
            if p.stderr:print(p.stderr,file=sys.stderr,flush=True)
            failed |= p.returncode!=0
        except (OSError,subprocess.TimeoutExpired) as error:
            print(str(error),file=sys.stderr,flush=True);failed=True
    return int(failed)
