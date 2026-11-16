"""Exercise a relocated, read-only source volume in the real dependency image."""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

from test_mounted_node_source import repositories, config_at, delivered, PROJECT
from scripts import node_git_source as source


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image',required=True)
    args=parser.parse_args()
    with tempfile.TemporaryDirectory(prefix='jswarm-mounted-container-') as temporary:
        root=Path(temporary)
        checkout=repositories(root);config=config_at(root)
        for name in ('launcher_commands.py','launcher_cache.py','launcher_streams.py',
                     'swarm_filesystem.py','run_input_cache.py'):
            shutil.copyfile(PROJECT/'scripts'/name,checkout/'JSwarm/scripts'/name)
        shutil.copytree(PROJECT/'ramtop/ramtop',checkout/'JSwarm/ramtop/ramtop',
                        ignore=shutil.ignore_patterns('__pycache__','*.test-log*'))
        probe=checkout/'JSwarm/scripts/launcher_peers.py'
        probe.write_text('''import json,os,pathlib
assert pathlib.Path(__file__).is_relative_to('/run/launcher-config/node-source/mounted-workspaces')
assert os.environ['SWARM_WORKSPACE']==str(pathlib.Path(__file__).parents[2])
import launcher_agent as agent
assert not agent.STREAM_RUNTIME_SOURCE and not agent.PERFORMANCE_RUNTIME_B64
agent.load_command_runtime()
agent.load_cache_runtime()
agent.load_filesystem_runtime()
agent.load_stream_runtime('/tmp/streams')
agent.load_performance_runtime()
print(json.dumps({'revision':os.environ['JSWARM_SOURCE_REVISION'],'file':__file__}),flush=True)
''')
        source.git(checkout/'JSwarm','add','.');source.git(checkout/'JSwarm','commit','-qm','container probe')
        manifest,revision=delivered(checkout,config)
        source.stage_source(manifest,config,revision)
        code='''from pathlib import Path
import sys
from launcher_peer_supervisor import verify_release,launch_source
releases=Path('/run/launcher-config/agent-releases')
folder=(releases/'current').resolve()
verify_release(folder,releases)
raise SystemExit(launch_source(folder,releases,sys.executable,'lifetech-').wait(timeout=15))
'''
        result=subprocess.run(['docker','run','--rm','--network','none','--read-only',
            '--user',str(os.getuid())+':'+str(os.getgid()),'--tmpfs','/tmp',
            '--mount',f'type=bind,src={config.parent},dst=/run/launcher-config,readonly',
            '--mount',f'type=bind,src={PROJECT / "scripts"},dst=/bootstrap,readonly',
            '--env','PYTHONPATH=/bootstrap','--env','HOME=/tmp',
            '--env','GIT_OPTIONAL_LOCKS=0','--entrypoint','python',args.image,'-B','-c',code],
            capture_output=True,text=True,timeout=90)
        if result.returncode:raise RuntimeError(result.stderr)
        observed=json.loads(result.stdout)
        assert observed['revision']==revision
        print(json.dumps({'passed':True,'image':args.image,'network':'none',
                          'source_mount':'read-only','revision':revision}))


if __name__=='__main__':main()
