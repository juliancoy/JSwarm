"""Read-only, bounded Docker observations on saved LAN hosts."""
import json
import re
import subprocess
import threading
import time
from monitor_access import targets, ssh

SCRIPT = '''import json,subprocess
selected=SELECTED
def docker(*args):
 return subprocess.run(['docker',*args],capture_output=True,text=True,errors='replace',timeout=5,check=True).stdout
try:
 ids=docker('ps','-q','--no-trunc').split()
 containers=[]
 for raw in (json.loads(docker('inspect',*ids)) if ids else []):
  ident=raw['Id']
  containers.append({'id':ident,'name':raw['Name'].lstrip('/'),'image':raw['Config']['Image'],
   'state':raw['State']['Status'],'started_at':raw['State'].get('StartedAt'),
   'health':raw['State'].get('Health',{}).get('Status'),'ports':raw['NetworkSettings'].get('Ports',{}),
   'restart_count':raw.get('RestartCount',0)})
 detail=next((item.copy() for item in containers if item['id']==selected),None)
 if detail:
  try:
   result=subprocess.run(['docker','logs','--timestamps','--tail','100',selected],capture_output=True,text=True,errors='replace',timeout=5)
   detail['console']=(result.stdout+'\\n'+result.stderr)[-48000:]
   detail['console_available']=result.returncode==0
  except subprocess.TimeoutExpired:
   detail.update(console='Console read timed out',console_available=False)
 print(json.dumps({'status':'live','containers':containers,'detail':detail}))
except (subprocess.SubprocessError,OSError):
 print(json.dumps({'status':'unavailable','containers':[],'detail':None}))
'''


class ContainerMonitor:
    def __init__(self):
        self.lock=threading.Lock()
        self.cache={}

    def view(self, host, ident=''):
        inventory=[{'host':'compai.local','local':True}]+targets()
        target=next((item for item in inventory if item['host']==host),None)
        if not target or ident and not re.fullmatch(r'[a-f0-9]{64}',ident):
            raise ValueError('Select a saved host and a valid container ID')
        key=(host,ident)
        with self.lock:
            cached=self.cache.get(key)
            if cached and time.monotonic()-cached[0]<3:
                return cached[1]
        script=SCRIPT.replace('SELECTED',repr(ident))
        try:
            result=subprocess.run(['python3','-c',script] if target.get('local') else ssh(target)+['python3 -'],
                input=None if target.get('local') else script,capture_output=True,text=True,timeout=12,check=True)
            data=json.loads(result.stdout)
        except (OSError,ValueError,subprocess.SubprocessError):
            data={'status':'unavailable','containers':[],'detail':None}
        data.update(host=host,sampled_at=time.time())
        with self.lock:
            if len(self.cache)>100:self.cache.clear()
            self.cache[key]=(time.monotonic(),data)
        return data
