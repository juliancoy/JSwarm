"""LAN owner controls for enrolled workers; never control arbitrary services."""
from concurrent.futures import ThreadPoolExecutor
import json
import threading
import subprocess
from pathlib import Path
from interface_metrics import capture
from agent_client import CONFIG

LOCK = threading.Lock()
SCRIPT = r'''import json, pathlib, subprocess, time
p=pathlib.Path.home()/'.config/lifetech-launcher/worker-policy.json'
marker=p.parent/'owner-stopped-workers.json'
def run(args):
 r=subprocess.run(args,capture_output=True,text=True,timeout=20)
 if r.returncode:raise RuntimeError(r.stderr.strip() or 'Worker command failed')
 return r.stdout
if ACTION in ('pause','stop','resume'):
 p.parent.mkdir(parents=True,exist_ok=True)
 t=p.with_suffix('.tmp');t.write_text(json.dumps({'mode':'auto' if ACTION=='resume' else 'paused'}));t.chmod(0o600);t.replace(p)
if ACTION=='stop':
 stopped=[]
 # Stop only named swarm compute services and labelled worker containers.
 unit='venter-peer-qc.service'
 active=subprocess.run(['systemctl','--user','is-active','--quiet',unit]).returncode==0
 if active:run(['systemctl','--user','stop',unit])
 ids=run(['docker','ps','-q','--filter','label=org.lifetech.worker=true']).split()
 for ident in ids:
  v=json.loads(run(['docker','inspect',ident]))[0]
  labels=v['Config'].get('Labels') or {}
  if labels.get('org.lifetech.gpu-qc-service')=='true':stopped.append(ident)
  run(['docker','stop','-t','5',ident])
 old=json.loads(marker.read_text()) if marker.exists() else {}
 marker.write_text(json.dumps({'gpu_containers':list(set(stopped+old.get('gpu_containers',[]))), 'peer_qc':active or old.get('peer_qc',False)}));marker.chmod(0o600)
if ACTION=='resume':
 saved=json.loads(marker.read_text()) if marker.exists() else {}
 if saved.get('peer_qc'):run(['systemctl','--user','start','venter-peer-qc.service'])
 marker.unlink(missing_ok=True)
mode=json.loads(p.read_text()).get('mode','auto') if p.exists() else 'auto'
ids=run(['docker','ps','-q','--filter','label=org.lifetech.worker=true']).split()
print(json.dumps({'mode':mode,'active_workers':len(ids),'checked_at':time.time()}))
'''

def enrolled():
 c=json.loads(CONFIG.read_text())
 return [{'host':name,'local':name==c['name']} for name in c['peers']]

def operate(host, action):
 if action not in ('status','pause','stop','resume'):raise ValueError('Unknown worker action')
 target=next((h for h in enrolled() if h['host']==host),None)
 if target is None:raise ValueError('Machine is not enrolled')
 result=capture(target,'ACTION='+repr(action)+'\n'+SCRIPT,timeout=90 if action!='status' else 5)
 if action in ('stop','resume'):supervise_gpu(host,action)
 return {'host':host,**result}

def snapshot():
 def status(target):
  try:return operate(target['host'],'status')
  except Exception:return {'host':target['host'],'mode':'unavailable','active_workers':None}
 with ThreadPoolExecutor(max_workers=5) as pool:return {'hosts':list(pool.map(status,enrolled()))}


GPU_UNITS={
 'compai.local':'lifetech-compai-gpu-qc.service',
 'codecollective.local':'lifetech-codecollective-gpu-qc.service',
 'julian-latitude.local':'lifetech-latitude-gpu-qc.service',
 'julian-inspiron-3793.local':'lifetech-inspiron-gpu-qc.service',
 'julian-minipc.local':'lifetech-gpu-qc.service',
}

def supervise_gpu(host, action):
    # Restart the lease supervisor, never an old container with an expired lease.
    unit=GPU_UNITS.get(host)
    if not unit:return
    subprocess.run(['systemctl','--user','stop' if action=='stop' else 'start',unit],
                   capture_output=True,text=True,check=True,timeout=100)
