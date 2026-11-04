"""Checksummed optional JSwarm components in the existing fleet publication."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

FILES=('scripts/__init__.py','scripts/swarm_paged_cache.py','scripts/swarm_runtime.py',
       'scripts/swarm_shared_cache.py','scripts/swarm_command.py',
       'docker_utils/__init__.py','docker_utils/images.py','docker_utils/policy.py',
       'swarm-control/server/agent_client.py','swarm-control/server/agent_updates.py',
       'swarm-control/server/deployment_components.py')


def digest(files):return hashlib.sha256(json.dumps(files,sort_keys=True,separators=(',',':')).encode()).hexdigest()


def snapshot(root):
    files={name:(Path(root)/name).read_text() for name in FILES}
    value={'schema':'jswarm-pager-component.v1','sha256':digest(files),'files':files}
    validate(value);return {'public-pager':value}


def validate(value):
    if value.get('schema')!='jswarm-pager-component.v1' or set(value['files'])!=set(FILES):raise ValueError('Incomplete pager component')
    if digest(value['files'])!=value.get('sha256'):raise ValueError('Pager component checksum mismatch')
    for name,source in value['files'].items():compile(source,name,'exec')
    return value


def deploy(value,names):
    """Execute only the immutable published snapshot, never workspace sources."""
    validate(value)
    base=Path.home()/'.local/state/swarm-control/node-components';base.mkdir(parents=True,exist_ok=True,mode=0o700)
    target=base/value['sha256']
    if not target.exists():
        with tempfile.TemporaryDirectory(prefix='.staging-',dir=base) as temporary:
            folder=Path(temporary)
            for name,source in value['files'].items():
                path=folder/name;path.parent.mkdir(parents=True,exist_ok=True,mode=0o700);path.write_text(source);path.chmod(0o600)
            folder.rename(target)
    for name,source in value['files'].items():
        if (target/name).read_text()!=source:raise ValueError('Installed pager component changed')
    code="import json,sys;from scripts.swarm_paged_cache import PLAN,deploy_fleet;print(json.dumps(deploy_fleet(names=json.loads(sys.argv[1])) if PLAN.exists() else {}))"
    env={**os.environ,'PYTHONPATH':str(target)+os.pathsep+str(target/'swarm-control/server')}
    result=subprocess.run([sys.executable,'-c',code,json.dumps(names)],cwd=target,env=env,capture_output=True,text=True,timeout=1200)
    if result.returncode:raise RuntimeError('Published pager component failed: '+result.stderr[-600:])
    return json.loads(result.stdout)
