"""Publish the pipeline through the standard main-node release."""
import json
from pathlib import Path
import shutil
import subprocess
import tempfile

ROOT=(Path(__file__).resolve().parents[2] if Path(__file__).resolve().parents[1].name in ('HarborGenetics', 'JSwarm') else Path(__file__).resolve().parents[1])
ASSETS=('node-web.js','pipeline.html','pipeline.css','pipeline.js','machine-controls.js','pipeline-model.js','pipeline-colors.js','levels-model.js','tasks.html')


def atomic_copy(source,target):
    with tempfile.NamedTemporaryFile(dir=target.parent,delete=False) as handle:
        temporary=Path(handle.name)
        handle.write(source.read_bytes())
    temporary.chmod(0o644);temporary.replace(target)


def authenticated_nodes(source,view,server):
    """Publish the owner API and node projections with file-level rollback."""
    import ast
    import time
    api=Path(subprocess.check_output(['systemctl','--user','show','swarm-control-api.service','--property=WorkingDirectory','--value'],text=True).strip())
    if not api.is_dir():raise RuntimeError('Authenticated API source not found')
    files={}
    for name in ('agent_client.py','node_pipeline_api.py','pipeline_nodes.py','task_machines.py','machine_controls.py','pipeline_workers.py'):
        files[server/name]=(source/name).read_bytes()
    files[api/'api.py']=(source/'api.py').read_bytes()
    files[api/'node_enrollment.py']=(source/'node_enrollment.py').read_bytes()
    files[api/'agent_client.py']=(source/'agent_client.py').read_bytes()
    for target in set((view,api/'view')):
        for name in ('pipeline.html','pipeline.js','pipeline.css','pipeline-model.js','pipeline-colors.js','levels-model.js','task-display.js','tasks.css','machine-controls.js','node-web.js','join-node.html','join-node.js'):
            files[target/name]=(source/'view'/name).read_bytes()
    # Preserve collector and unrelated routes in the active dashboard release.
    installed=(server/'task_dashboard.py').read_text()
    tree=ast.parse(installed)
    handler=next(n for n in tree.body if isinstance(n,ast.ClassDef) and n.name=='Handler')
    get=next(n for n in handler.body if isinstance(n,ast.FunctionDef) and n.name=='do_GET')
    branch=next(n for n in ast.walk(get) if isinstance(n,ast.If) and ast.unparse(n.test)=="path.path == '/api/tasks/pipeline'")
    lines=installed.splitlines(keepends=True);first=branch.body[0].lineno-1;last=branch.body[-1].end_lineno
    installed=''.join(lines[:first])+"                from pipeline_nodes import snapshot as pipeline_snapshot\n                data=pipeline_snapshot()\n"+''.join(lines[last:])
    files[server/'task_dashboard.py']=installed.encode()
    for path,data in files.items():
        if path.suffix=='.py':compile(data,str(path),'exec')
    backup=Path.home()/'.local/state/swarm-control/authenticated-pipeline'/str(time.time_ns())
    backup.mkdir(parents=True,mode=0o700)
    original={path:path.read_bytes() if path.exists() else None for path in files}
    (backup/'manifest.json').write_text(json.dumps({str(i):str(path) for i,path in enumerate(original)}))
    for i,(path,data) in enumerate(original.items()):
        if data is not None:(backup/str(i)).write_bytes(data)
    def write(path,data):
        with tempfile.NamedTemporaryFile(dir=path.parent,delete=False) as handle:
            temporary=Path(handle.name);handle.write(data)
        temporary.chmod(0o644);temporary.replace(path)
    try:
        for path,data in files.items():write(path,data)
        subprocess.run(['systemctl','--user','restart','swarm-tasks.service','swarm-control-api.service'],check=True)
        for name in ('swarm-tasks.service','swarm-control-api.service'):
            subprocess.run(['systemctl','--user','is-active','--quiet',name],check=True)
    except BaseException:
        for path,data in original.items():
            if data is None:path.unlink(missing_ok=True)
            else:write(path,data)
        subprocess.run(['systemctl','--user','restart','swarm-tasks.service','swarm-control-api.service'],check=False)
        raise
    print('Authenticated node pipeline and PIdP/PKCE enrollment deployed; backup: '+str(backup))
    return 0


def manage_node_view(action,page='pipeline.html'):
    """All public views follow the checksummed main-node publication."""
    import sys
    from urllib.request import urlopen
    if action=='status':
        with urlopen('http://127.0.0.1/api/jswarm-web',timeout=10) as response:context=json.load(response)
        print(json.dumps({'url':'http://jswarm.localhost/'+page,'node':context['node'],'deployed':True}))
        return 0
    project=Path(__file__).resolve().parents[1]
    for arguments in (['--publish'],[]):
        subprocess.run([sys.executable,'-m','scripts.deploy_swarm_inputs',*arguments],cwd=project,check=True)
    print('Node UI deployed: http://jswarm.localhost/'+page+'; compute workers retained')
    return 0


def manage(action):
    if action!='authenticated-nodes':return manage_node_view(action)
    # The authenticated WAN API is a backend integration, independent of the
    # node-served frontend and the retired nginx container.
    unit=subprocess.check_output(['systemctl','--user','show','swarm-tasks.service','--property=WorkingDirectory','--value'],text=True).strip()
    server=Path(unit)
    if not server.is_dir():raise RuntimeError('Task dashboard source not found')
    source=ROOT/'swarm-control/server'
    return authenticated_nodes(source,source/'view',server)
