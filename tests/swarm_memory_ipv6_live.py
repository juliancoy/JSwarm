"""Verify an enrolled source over real scoped IPv6 with IPv4 excluded in this test."""
import argparse
import datetime
import json
import subprocess
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from scripts.deploy_swarm_memory import execute, CONFIG

PROBE=r'''
import concurrent.futures,hashlib,json,pathlib,socket,sys
p=json.load(sys.stdin)
release=pathlib.Path.home()/'.local/share/swarm-memory'/p['record']['revision']
sys.path[:0]=[str(release),str(release/'swarm-control/server')]
from scripts import swarm_immutable_read as module
config=json.loads((release/'config.json').read_text());source=config['sources'][0]
port=json.loads((pathlib.Path.home()/'.local/state/lifetech-launcher/bulk-transport/config.json').read_text())['port']
pool=concurrent.futures.ThreadPoolExecutor(max_workers=1)
try:
 rows=pool.submit(socket.getaddrinfo,source['source'],port,socket.AF_UNSPEC,socket.SOCK_STREAM).result(timeout=20)
finally:pool.shutdown(wait=False,cancel_futures=True)
rows=[row for row in rows if row[0]==socket.AF_INET6]
if not rows:raise RuntimeError('No IPv6 source route discovered; IPv4 health is separate')
# Exclude IPv4 only inside this qualification process. The standard connector
# must preserve the discovered IPv6 scope and retain all TLS/pin/range checks.
socket.getaddrinfo=lambda *args,**kwargs:rows
module.service_candidates=lambda *args:rows
reader=module.Reader({'object':source})
data=reader.read('object',0,config['object']['page_bytes'])
assert hashlib.sha256(data).hexdigest()==config['object']['pages'][0]
assert reader.stats['ipv6_connections']==1
print(json.dumps({'passed':True,'ipv4_excluded_for_test':True,'transport':dict(reader.stats),'verified_bytes':len(data)}))
'''

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--deployment',type=Path,required=True)
    parser.add_argument('--node',required=True);parser.add_argument('--report',type=Path,required=True)
    args=parser.parse_args();plan=json.loads(args.deployment.read_text())
    payload={'record':plan['nodes'][args.node]}
    if args.node==json.loads(CONFIG.read_text())['name']:
        # The FUSE service runs on this host, whose NSS/mDNS modules differ
        # from the minimal owner command capsule. Test its actual environment.
        process=subprocess.run([sys.executable,'-c',PROBE],input=json.dumps(payload),capture_output=True,text=True,timeout=45)
        if process.returncode:raise RuntimeError(process.stderr)
        result=json.loads(process.stdout);result['environment']='mount-service host'
    else:
        result=execute(args.node,PROBE,payload,timeout=45);result['environment']='owner command capsule'
    result.update(date=datetime.datetime.now(datetime.timezone.utc).isoformat(),node=args.node,object_id=plan['object_id'])
    args.report.write_text(json.dumps(result,indent=2));print(json.dumps(result))
