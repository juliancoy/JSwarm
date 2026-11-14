"""Checksummed optional JSwarm components in the existing fleet publication."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

FILES=('scripts/__init__.py','scripts/launcher_discovery.py','scripts/swarm_paged_cache.py','scripts/swarm_runtime.py',
       'scripts/launcher_streams.py','scripts/swarm_stream_work.py','scripts/swarm_gpu_runtime.py',
       'scripts/swarm_shared_cache.py','scripts/swarm_command.py',
       'docker_utils/__init__.py','docker_utils/images.py','docker_utils/policy.py',
       'swarm-control/server/agent_client.py','swarm-control/server/agent_updates.py',
       'swarm-control/server/deployment_components.py')

FILESYSTEM_FILES=tuple(dict.fromkeys((*FILES, 'scripts/deploy_swarm_filesystem.py',
    'scripts/swarm_filesystem.py','scripts/swarm_fs_mount.py','scripts/run_input_cache.py',
    'scripts/launcher_discovery.py','scripts/swarm_host_service.py')))


def digest(files):return hashlib.sha256(json.dumps(files,sort_keys=True,separators=(',',':')).encode()).hexdigest()


def snapshot(root):
    files={name:(Path(root)/name).read_text() for name in FILES}
    value={'schema':'jswarm-pager-component.v1','sha256':digest(files),'files':files}
    validate(value)
    filesystem={'schema':'jswarm-filesystem-component.v1',
                'files':{name:(Path(root)/name).read_text() for name in FILESYSTEM_FILES}}
    filesystem['sha256']=digest(filesystem['files']);validate(filesystem)
    return {'public-pager':value,'private-filesystem':filesystem}


def validate(value, *, historical=False):
    expected={'jswarm-pager-component.v1':FILES,'jswarm-filesystem-component.v1':FILESYSTEM_FILES}.get(value.get('schema'))
    # History is read for generation accounting, never executed. Its file list
    # belongs to that release, so do not maintain per-version layout exceptions.
    if expected is None:raise ValueError('Unknown deployment component schema')
    if not historical and set(value['files'])!=set(expected):raise ValueError('Incomplete deployment component')
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
    module='swarm_paged_cache' if value['schema']=='jswarm-pager-component.v1' else 'deploy_swarm_filesystem'
    code="import json,sys;from scripts."+module+" import PLAN,deploy_fleet;print(json.dumps(deploy_fleet(names=json.loads(sys.argv[1])) if PLAN.exists() else {}))"
    env={**os.environ,'PYTHONPATH':str(target)+os.pathsep+str(target/'swarm-control/server')}
    result=subprocess.run([sys.executable,'-c',code,json.dumps(names)],cwd=target,env=env,capture_output=True,text=True,timeout=1200)
    if result.returncode:raise RuntimeError('Published pager component failed: '+result.stderr[-600:])
    return json.loads(result.stdout)
