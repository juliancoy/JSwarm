"""Exercise the exact portable DNS-SD dependency bundle on an enrolled node."""
import argparse
import base64
import json
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from scripts.deploy_swarm_memory import execute,PROJECT

PROBE=r'''
import base64,json,pathlib,sys,tempfile
p=json.load(sys.stdin)
with tempfile.TemporaryDirectory(prefix='discovery-proof-') as folder:
 root=pathlib.Path(folder)
 for name,value in p['files'].items():
  target=root/name;target.parent.mkdir(exist_ok=True,parents=True);target.write_bytes(base64.b64decode(value))
 sys.path[:0]=[str(root),*map(str,root.glob('*.whl'))]
 from scripts.swarm_immutable_read import service_candidates
 rows=service_candidates(p['peer'],p['port'],3)
 assert rows,'No admitted DNS-SD routes'
 print(json.dumps({'passed':True,'peer':p['peer'],'routes':rows}))
'''

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--node',required=True);parser.add_argument('--peer',required=True)
    parser.add_argument('--wheels',type=Path,required=True);parser.add_argument('--report',type=Path,required=True)
    args=parser.parse_args()
    files={name:(PROJECT/name).read_bytes() for name in ['scripts/__init__.py','scripts/swarm_immutable_read.py','scripts/launcher_discovery.py']}
    files.update({p.name:p.read_bytes() for p in args.wheels.glob('*.whl')})
    result=execute(args.node,PROBE,{'files':{n:base64.b64encode(data).decode() for n,data in files.items()},'peer':args.peer,'port':9450},timeout=30)
    args.report.write_text(json.dumps(result,indent=2));print(json.dumps(result))
