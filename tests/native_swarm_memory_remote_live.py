"""Verify a native peer-only read from an enrolled remote execution environment."""
import argparse
import datetime
import json
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from scripts.deploy_swarm_memory import execute

PROBE=r'''
import hashlib,json,os,pathlib,subprocess,sys,tempfile
p=json.load(sys.stdin);release=pathlib.Path(p['record']['release'])
config=json.loads((release/'config.json').read_text());obj=config['objects'][0]['manifest']
if p['peer'] not in config['objects'][0]['peers']:raise ValueError('Unconfigured peer')
config['objects'][0].update(sources=[],peers=[p['peer']]);config['replicas']=0
with tempfile.TemporaryDirectory(prefix='native-peer-proof-') as folder:
 path=pathlib.Path(folder)/'config.json';path.write_text(json.dumps(config))
 result=subprocess.run([str(release/'bin/swarm-memory'),'--config',str(path),'--read',obj['id'],'0',str(obj['page_bytes'])],
                       env={**os.environ,'LD_LIBRARY_PATH':str(release/'lib')},capture_output=True,timeout=30)
 if result.returncode:raise RuntimeError(result.stderr.decode())
 assert hashlib.sha256(result.stdout).hexdigest()==obj['pages'][0]
 stats=json.loads(result.stderr);assert stats['counters']['peer_hits']==1 and stats['counters'].get('source_requests',0)==0
 print(json.dumps({'passed':True,'source_disabled':True,'peer':p['peer'],'metrics':stats}))
'''

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--deployment',type=Path,required=True);parser.add_argument('--report',type=Path,required=True)
    parser.add_argument('--node',required=True);parser.add_argument('--peer',required=True);args=parser.parse_args()
    plan=json.loads(args.deployment.read_text())
    result=execute(args.node,PROBE,{'record':plan['nodes'][args.node],'peer':args.peer},timeout=60)
    result.update(date=datetime.datetime.now(datetime.timezone.utc).isoformat(),node=args.node,revision=plan['nodes'][args.node]['revision'])
    args.report.write_text(json.dumps(result,indent=2));print(json.dumps(result,indent=2))
