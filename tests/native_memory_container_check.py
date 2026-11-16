"""Verify deployed immutable reference pages from a restricted compute container."""
import argparse
import datetime
import json
import os
from pathlib import Path
import subprocess

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--deployment', type=Path, required=True)
parser.add_argument('--report', type=Path, required=True)
args = parser.parse_args()
plan = json.loads(args.deployment.read_text())
record = plan['nodes']['compai.local']
publication = json.loads(Path(plan['publication']).read_text())
obj = publication['object']
probe = '''import hashlib,json,mmap,sys
p=json.load(sys.stdin)
with open('/reference','rb') as f, mmap.mmap(f.fileno(),0,access=mmap.ACCESS_READ) as m:
 assert len(m)==p['bytes']
 for i in (0,1,len(p['pages'])//2,len(p['pages'])-1):
  assert hashlib.sha256(m[i*p['page_bytes']:min((i+1)*p['page_bytes'],len(m))]).hexdigest()==p['pages'][i]
print('four distant reference pages verified')
'''
command = ['docker','run','--rm','--pull=never','--network=none','--read-only',
           '--user',str(os.getuid())+':'+str(os.getgid()),
           '--memory=256m','--memory-swap=256m','--cap-drop=ALL',
           '--security-opt=no-new-privileges','--mount',
           'type=bind,src='+record['path']+',dst=/reference,readonly',
           '-i','genomeharbor/epigenetic-mmap:20261115','python3','-c',probe]
result = subprocess.run(command, input=json.dumps(obj), capture_output=True, text=True, timeout=120)
report = {'date':datetime.datetime.now(datetime.timezone.utc).isoformat(),
          'revision':record['revision'],'binary_sha256':record['binary_sha256'],
          'passed':result.returncode==0,'stdout':result.stdout,'stderr':result.stderr}
args.report.write_text(json.dumps(report,indent=2))
print(json.dumps(report,indent=2))
if result.returncode:raise SystemExit(result.returncode)
record.update(ready=True,verification=str(args.report.resolve()))
temporary=args.deployment.with_suffix('.tmp')
temporary.write_text(json.dumps(plan,indent=2));temporary.replace(args.deployment)
