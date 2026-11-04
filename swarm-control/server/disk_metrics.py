"""Physical disk read/write telemetry, separate from Linux I/O busy time."""
import threading
import tempfile
import subprocess
from collections import deque
import json
from pathlib import Path
from interface_metrics import capture

PROFILE_LOCK=threading.Lock()
CAPACITIES=Path.home()/'.config/swarm-control/disk-capacities.json'
SCRIPT=r'''import json,time,hashlib
from pathlib import Path
rows={}
for device in Path('/sys/block').iterdir():
 if '/virtual/' in str(device.resolve()):continue
 try:
  stat=[int(value) for value in (device/'stat').read_text().split()]
  size=int((device/'size').read_text())*512
  if size<=0:continue
  try:
   if (device/'device/type').read_text().strip()=='5':continue
  except OSError:pass
  identity=''
  for key in ('device/wwid','device/serial','wwid'):
   try:identity=(device/key).read_text().strip();break
   except OSError:pass
  model=''
  try:model=(device/'device/model').read_text().strip()
  except OSError:pass
  rows[device.name]={'identity':hashlib.sha256((device.name+':'+str(size)+':'+identity+':'+model).encode()).hexdigest(),'model':model,'read_bytes':stat[2]*512,'write_bytes':stat[6]*512,'busy_ms':stat[9]}
 except (OSError,ValueError,IndexError):continue
print(json.dumps({'monotonic':time.monotonic(),'sampled_at':time.time(),'boot_id':Path('/proc/sys/kernel/random/boot_id').read_text().strip(),'disks':rows}))
'''


class DiskSampler:
    def __init__(self,reader=None,capacities=CAPACITIES):
        self.reader=reader or (lambda target:capture(target,SCRIPT));self.previous=None;self.capacities=Path(capacities);self.windows={}

    def sample(self,target):
        try:current=self.reader(target)
        except (OSError,ValueError,TypeError,KeyError,RuntimeError,subprocess.SubprocessError):return []
        previous=self.previous;self.previous=current
        try:profiles=json.loads(self.capacities.read_text())
        except (OSError,ValueError):profiles={}
        elapsed=current['monotonic']-previous['monotonic'] if previous else 0
        valid=bool(previous and previous['boot_id']==current['boot_id'] and 0<elapsed<=30)
        rows=[]
        for name,after in list(current['disks'].items())[:32]:
            profile=profiles.get(target['host'],{}).get(after['identity'],{})
            capacity=profile.get('total_bytes_per_second')
            calibrated=isinstance(capacity,(int,float)) and not isinstance(capacity,bool) and 0<capacity<1e13 and profile.get('busy_percent',0)>=50
            row={'name':name,'model':after.get('model',''),'identity':after['identity'],'sampled_at':current['sampled_at'],'status':'warming',
                 'read_bytes_per_second':None,'write_bytes_per_second':None,'read_percent':None,'write_percent':None,'total_percent':None,'busy_percent':None,
                 'capacity_basis':profile.get('basis') if calibrated else None}
            if calibrated:row['capacity_bytes_per_second']=capacity
            before=previous.get('disks',{}).get(name) if valid else None
            if before and before.get('identity')==after['identity']:
                changes={key:after[key]-before[key] for key in ('read_bytes','write_bytes','busy_ms')}
                if min(changes.values())>=0:
                    row.update(status='live',busy_percent=round(min(100,100*changes['busy_ms']/elapsed/1000),1))
                    for direction in ('read','write'):row[direction+'_bytes_per_second']=round(changes[direction+'_bytes']/elapsed,1)
                    frames=self.windows.setdefault(after['identity'],deque())
                    frames.append((elapsed,changes['read_bytes']+changes['write_bytes'],elapsed*row['busy_percent']))
                    while len(frames)>1 and sum(frame[0] for frame in frames)-frames[0][0]>=30:frames.popleft()
                    duration=sum(frame[0] for frame in frames)
                    busy=sum(frame[2] for frame in frames)/duration
                    if duration>=30 and busy>=50:
                        observed=sum(frame[1] for frame in frames)/duration
                        if observed>0 and (not calibrated or observed>capacity):
                            capacity=observed;calibrated=True
                            row.update(capacity_basis='observed sustained',capacity_bytes_per_second=capacity)
                            self.save_profile(target['host'],after['identity'],capacity,current['sampled_at'],duration,busy)
                    if calibrated:
                        parts={direction:100*row[direction+'_bytes_per_second']/capacity for direction in ('read','write')}
                        row.update({direction+'_percent':round(value,1) for direction,value in parts.items()});row['total_percent']=round(sum(parts.values()),1)
                else:self.windows.pop(after['identity'],None)
            else:self.windows.pop(after['identity'],None)
            rows.append(row)
        return rows

    def save_profile(self,host,identity,capacity,sampled_at,window_seconds,busy_percent):
        try:
            with PROFILE_LOCK:
                try:profiles=json.loads(self.capacities.read_text())
                except (OSError,ValueError):profiles={}
                previous=profiles.setdefault(host,{}).get(identity,{})
                if previous.get('busy_percent',0)>=50 and previous.get('total_bytes_per_second',0)>=capacity:return
                profiles[host][identity]={'total_bytes_per_second':capacity,'basis':'observed sustained','sampled_at':sampled_at,'window_seconds':window_seconds,'busy_percent':busy_percent}
                self.capacities.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
                with tempfile.NamedTemporaryFile(mode='w',dir=self.capacities.parent,delete=False) as stream:
                    json.dump(profiles,stream);pending=Path(stream.name)
                pending.replace(self.capacities)
        except OSError:pass
