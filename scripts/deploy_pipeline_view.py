"""Publish the pipeline through the standard main-node release."""
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
    return manage_node_view(action)
