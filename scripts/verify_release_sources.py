"""Verify fleet release sources from committed adjacent checkouts, without deployment.

No credentials, workload files, service changes, downloads, or network are used.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile

FOLLOWUP=('genomics_followup_controller.py','genomics_followup_gpu_routing.py',
          'genomics_region_staging.py','wgs_preparation_queue.py','swarm_runtime.py','launcher_streams.py')
FINALIZER=('wgs_preparation_queue.py','wgs_bounded_finalize.py','wgs_finalize.py',
           'wgs_recall.py','wgs_call_summary.py','wgs_reference_confidence.py')


def check(workspace,tests=False):
    workspace=Path(workspace).resolve()
    commits={}
    with tempfile.TemporaryDirectory(prefix='swarm-committed-sources-') as temporary:
        target=Path(temporary)/'Organization';target.mkdir()
        for name in ('Organization','JSwarm','GenomeHarbor'):
            source=workspace if name=='Organization' else workspace/name
            destination=target if name=='Organization' else target/name
            destination.mkdir(exist_ok=True)
            commits[name]=subprocess.check_output(['git','-C',str(source),'rev-parse','HEAD'],text=True).strip()
            archive=Path(temporary)/(name+'.tar')
            subprocess.run(['git','-C',str(source),'archive','--format=tar','--output='+str(archive),'HEAD'],check=True)
            # Local git archives contain the versioned sibling-package symlinks.
            with tarfile.open(archive) as bundle:bundle.extractall(destination,filter='fully_trusted')
        env={**os.environ,'PYTHONPATH':os.pathsep.join(map(str,(target/'JSwarm/swarm-control/server',target/'JSwarm',target))),'PYTHONDONTWRITEBYTECODE':'1'}
        program="""
import hashlib,json
from pathlib import Path
from agent_updates import LauncherUpdates
from scripts import genomics_followup_controller,genomics_followup_gpu_routing,genomics_region_staging,wgs_preparation_queue,wgs_bounded_finalize,wgs_finalize,shard_runtime_sampler
u=LauncherUpdates(Path('JSwarm'),lambda:False,lambda *args:None)
print(json.dumps({'agent_revision':u.revision,'component_hashes':{k:v['sha256'] for k,v in u.components.items()}}))
"""
        release=json.loads(subprocess.check_output([sys.executable,'-c',program],cwd=target,env=env,text=True))
        for kind,names in (('followup',FOLLOWUP),('finalizer',FINALIZER)):
            source=target/'GenomeHarbor/scripts'
            hashes={name:hashlib.sha256((source/name).read_bytes()).hexdigest() for name in names}
            for name in names:compile((source/name).read_bytes(),name,'exec')
            release[kind+'_sources']=hashes
        if tests:
            suites=['JSwarm/tests/test_execution_accounting.py','JSwarm/tests/test_shard_runtime_sampler.py',
                    'JSwarm/swarm-control/server/tests/test_task_pipeline.py',
                    'GenomeHarbor/tests/test_genomics_followup_controller.py',
                    'GenomeHarbor/tests/test_genomics_followup_gpu_routing.py',
                    'GenomeHarbor/tests/test_genomics_region_staging.py',
                    'GenomeHarbor/tests/test_wgs_preparation_queue.py']
            subprocess.run([sys.executable,'-m','pytest','-q',*suites],cwd=target,env=env,check=True)
        return {'schema':'swarm-committed-source-verification.v1','commits':commits,'release':release,'tests':tests}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--workspace',type=Path,default=Path(__file__).resolve().parents[2])
    parser.add_argument('--tests',action='store_true',help='Run focused host/local Docker regression checks in the clean archive')
    parser.add_argument('--output',type=Path,help='Save operational source hashes and commit IDs')
    args=parser.parse_args();value=check(args.workspace,args.tests)
    encoded=json.dumps(value,indent=2)+'\n'
    if args.output:
        args.output.parent.mkdir(parents=True,exist_ok=True);args.output.write_text(encoded);args.output.chmod(0o600)
    print(encoded,end='')


if __name__=='__main__':main()
