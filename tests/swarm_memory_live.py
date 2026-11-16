"""Explicit fleet qualification of an installed object from compute containers."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import datetime
import fcntl
import hashlib
import json
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from scripts.deploy_swarm_memory import execute

PROBE=r'''
import hashlib,json,os,pathlib,subprocess,time
p=json.load(__import__('sys').stdin)
mount=pathlib.Path(p['record']['path'])
for _ in range(50):
 if mount.is_file():break
 time.sleep(.1)
if not mount.is_file():raise RuntimeError('Memory mount not ready')
image=subprocess.check_output(['docker','image','inspect','genomeharbor/epigenetic-mmap:20261115','--format','{{.Id}}'],text=True).strip()
security=subprocess.check_output(['docker','info','--format','{{json .SecurityOptions}}'],text=True)
user='0:0' if 'rootless' in security else str(os.getuid())+':'+str(os.getgid())
code="""import hashlib,json,mmap,pathlib,sys
probes=json.loads(sys.argv[1]);length=int(sys.argv[2]);result=[]
with open('/reference/object','rb') as f,mmap.mmap(f.fileno(),0,access=mmap.ACCESS_READ) as m:
 assert len(m)==length
 for offset,size,sha in probes:
  assert hashlib.sha256(m[offset:offset+size]).hexdigest()==sha
  result.append(offset)
try:open('/reference/object','r+b')
except OSError:pass
else:raise AssertionError('Object is writable')
print(json.dumps({'mapped_bytes':length,'verified_pages':len(result),'network':'none','read_only':True}))
"""
command=['docker','run','--rm','--pull=never','--network','none','--read-only','--cap-drop','ALL','--security-opt','no-new-privileges','--memory','128m','--memory-swap','128m','--pids-limit','16','--user',user,'--mount','type=bind,src='+str(mount)+',dst=/reference/object,readonly',image,'python3','-c',code,json.dumps(p['probes']),str(p['bytes'])]
r=subprocess.run(command,capture_output=True,text=True,timeout=90)
if r.returncode:raise RuntimeError(r.stderr[-1500:])
metrics=json.loads(pathlib.Path(p['record']['metrics']).read_text())
print(json.dumps({'container':json.loads(r.stdout),'metrics':metrics,'image':image,'user':user}))
'''


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--deployment',type=Path,required=True);parser.add_argument('--report',type=Path,required=True)
    parser.add_argument('--node',action='append');args=parser.parse_args()
    plan=json.loads(args.deployment.read_text());obj=json.loads(Path(plan['publication']).read_text())['object']
    revisions=sorted({row.get('revision','') for row in plan['nodes'].values()})
    # Exercise cold offsets for each transport release, rather than only pages
    # warmed by the previous release's check. Keep these reproducible in logs.
    cold={int(hashlib.sha256((str(revisions)+str(i)).encode()).hexdigest(),16)%len(obj['pages']) for i in range(3)}
    numbers=sorted({0,1,len(obj['pages'])//3,len(obj['pages'])//2,len(obj['pages'])-1,*cold})
    probes=[(i*obj['page_bytes'],min(obj['page_bytes'],obj['bytes']-i*obj['page_bytes']),obj['pages'][i]) for i in numbers]
    names=args.node or list(plan['nodes'])
    def check(name):
        try:
            result=execute(name,PROBE,{'record':plan['nodes'][name],'probes':probes,'bytes':obj['bytes']},timeout=120)
            assert result['container']['verified_pages']==len(probes)
            assert result['metrics']['object']==obj['id']
            return name,{'passed':True,**result}
        except Exception as error:return name,{'passed':False,'error':str(error)}
    with ThreadPoolExecutor(max_workers=5) as pool:results=dict(pool.map(check,names))
    record={'date':datetime.datetime.now(datetime.timezone.utc).isoformat(),'object':obj['id'],'results':results,
            'code_sha256':{str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in Path(__file__).resolve().parents[1].joinpath('scripts').glob('swarm_memory*.py')}}
    args.report.parent.mkdir(parents=True,exist_ok=True);args.report.write_text(json.dumps(record,indent=2))
    with args.deployment.with_suffix('.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        latest=json.loads(args.deployment.read_text())
        for name,result in results.items():
            if latest['nodes'][name].get('revision')!=plan['nodes'][name].get('revision'):
                raise ValueError('Memory mount changed during verification')
            latest['nodes'][name]['ready']=result['passed']
            latest['nodes'][name]['verification']=str(args.report)
        args.deployment.write_text(json.dumps(latest,indent=2))
    print(json.dumps(results,indent=2))
    return int(any(not r['passed'] for r in results.values()))

if __name__=='__main__':raise SystemExit(main())
