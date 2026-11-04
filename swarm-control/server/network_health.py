"""Supplement old launcher telemetry with read-only physical NIC health samples."""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import threading
import time
from interface_metrics import InterfaceSampler, SCRIPT, capture

TRANSFER_READ = r'''
transfers=[]
for p in sorted((Path.home()/'.local/state/lifetech-launcher/transfers').glob('*.json'),key=lambda p:p.stat().st_mtime,reverse=True)[:64]:
 try:
  v=json.loads(p.read_text())
  if 0<=time.time()-v.get('sampled_at',0)<900:
   fields=('id','task_id','source','destination','status','sampled_at','completed_bytes','total_bytes','transferred_bytes','bytes_per_second','average_bytes_per_second','elapsed_seconds')
   row={k:v[k] for k in fields if k in v}
   if time.time()-v['sampled_at']>15 and row.get('status')=='running':row.update(status='stale',bytes_per_second=None)
   transfers.append(row)
 except (OSError,ValueError,TypeError):pass
'''
HEALTH_SCRIPT=SCRIPT.replace("print(json.dumps({'monotonic'",TRANSFER_READ+"\nprint(json.dumps({'task_transfers':transfers,'monotonic'")


class NetworkHealth:
    def __init__(self):self.lock=threading.Lock();self.values={};self.samplers={}

    def sample(self,target):
        result={}
        def reader(t):
            value=capture(t,HEALTH_SCRIPT);result['transfers']=value.get('task_transfers',[]);return value
        sampler=self.samplers.setdefault(target['host'],InterfaceSampler(reader));sampler.reader=reader
        rows=sampler.sample(target)
        if rows:
            with self.lock:self.values[target['host']]={'interfaces':rows,'transfers':result.get('transfers',[])}

    def worker(self):
        from cpu import targets
        with ThreadPoolExecutor(max_workers=5) as pool:
            while True:
                try:
                    inventory=[{'host':'compai.local','local':True}]+[h for h in targets() if h['host']!='compai.local']
                    futures=[pool.submit(self.sample,h) for h in inventory]
                    for f in futures:
                        try:f.result()
                        except Exception:pass
                except (OSError,ValueError):pass
                time.sleep(5)

    def merge(self,hosts):
        with self.lock:values=dict(self.values)
        for host in hosts:
            value=values.get(host['host'],{});rows=value.get('interfaces',[])
            if rows and time.time()-rows[0]['sampled_at']<15:
                host['network_interfaces']=rows
                host['task_transfers']=value.get('transfers',[])
        return hosts
