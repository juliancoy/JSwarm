"""Read a real remote cached page with authoritative range reads disabled."""
import argparse
import datetime
import json
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from scripts.deploy_swarm_memory import execute

PROBE=r'''
import hashlib,json,pathlib,sys,tempfile
p=json.load(sys.stdin)
release=pathlib.Path.home()/'.local/share/swarm-memory'/p['record']['revision']
sys.path[:0]=[str(release),str(release/'swarm-control/server')]
from scripts.swarm_memory_mount import ObjectView
from scripts.run_input_cache import InputCache
from scripts.swarm_fs_mount import Client
config=json.loads((release/'config.json').read_text())
# The signed command capsule exposes /dev/shm read-only and already provides
# a private bounded /tmp tmpfs. Verify it before storing any test page there.
assert any(row.split()[4]=='/tmp' and row.split(' - ',1)[1].split()[0]=='tmpfs' for row in pathlib.Path('/proc/self/mountinfo').read_text().splitlines())
with tempfile.TemporaryDirectory(prefix='swarm-memory-peer-proof-',dir='/tmp') as folder:
 cache=InputCache(folder,budget=1024**2,reserve=0)
 view=ObjectView(config,cache=cache,peer_client=Client(config['filesystem'],cache=cache))
 def unavailable(*args):raise OSError('Qualification: authoritative range transport disabled')
 view.pages.fetch=unavailable
 data=view.pages.read(0,config['object']['page_bytes'])
 assert hashlib.sha256(data).hexdigest()==config['object']['pages'][0]
 assert view.pages.stats['peer_hits']>0 and view.pages.stats['source_reads']==0
 view.pages.close()
 print(json.dumps({'passed':True,'simulated_source_outage':True,'counters':dict(view.pages.stats)}))
'''

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--deployment',type=Path,required=True);parser.add_argument('--node',required=True)
    parser.add_argument('--report',type=Path,required=True);args=parser.parse_args()
    plan=json.loads(args.deployment.read_text())
    result=execute(args.node,PROBE,{'record':plan['nodes'][args.node]},timeout=30)
    result.update(date=datetime.datetime.now(datetime.timezone.utc).isoformat(),node=args.node,object_id=plan['object_id'])
    args.report.write_text(json.dumps(result,indent=2));print(json.dumps(result))
