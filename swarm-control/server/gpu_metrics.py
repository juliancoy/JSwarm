"""Read GPU activity on enrolled hosts; never count software renderers as GPUs."""
import subprocess
from interface_metrics import capture

SCRIPT=r'''import json,time,re
from pathlib import Path
cards={};clients={}
for card in Path('/sys/class/drm').glob('card[0-9]*'):
 if '-' in card.name:continue
 try:
  device=(card/'device').resolve();pci=device.name
  if not re.fullmatch(r'[0-9a-f]{4}:[0-9a-f]{2}:[0-9a-f]{2}\.[0-7]',pci):continue
  row={'pci_id':pci,'vendor_id':(device/'vendor').read_text().strip()}
  for file,key in [('gpu_busy_percent','busy'),('mem_info_vram_total','memory_total'),('mem_info_vram_used','memory_used')]:
   try:row[key]=int((device/file).read_text())
   except (OSError,ValueError):pass
  cards[pci]=row
 except OSError:pass
reads=0
for proc in Path('/proc').glob('[0-9]*'):
 if reads>=4096:break
 try:
  for fd in (proc/'fd').iterdir():
   if reads>=4096:break
   try:
    if not str(fd.readlink()).startswith('/dev/dri/'):continue
    reads+=1;text=(proc/'fdinfo'/fd.name).read_text()
    values=dict(line.split(':',1) for line in text.splitlines() if ':' in line)
    pci=values.get('drm-pdev','').strip();ident=values.get('drm-client-id','').strip()
    if pci not in cards or not ident:continue
    engines={}
    for key,value in values.items():
     if key.startswith('drm-engine-') and not key.startswith('drm-engine-capacity-'):
      match=re.fullmatch(r'\s*(\d+) ns\s*',value)
      if match:engines[key[11:]]=int(match.group(1))
    clients[pci+':'+ident]={'pci_id':pci,'engines':engines,'capacity':{k[20:]:int(v) for k,v in values.items() if k.startswith('drm-engine-capacity-') and v.strip().isdigit()}}
   except (OSError,ValueError):pass
 except OSError:pass
print(json.dumps({'monotonic':time.monotonic(),'boot_id':Path('/proc/sys/kernel/random/boot_id').read_text().strip(),'cards':cards,'clients':clients}))
'''


def activity(previous,current):
    elapsed=current['monotonic']-previous['monotonic'] if previous else 0
    valid=previous and current.get('boot_id')==previous.get('boot_id') and 0<elapsed<=30
    results={}
    for pci,card in current.get('cards',{}).items():
        row={}
        if card.get('busy') is not None:
            row.update(utilization_percent=max(0,min(100,card['busy'])),utilization_basis='Driver GPU busy counter')
        elif valid:
            engines={};capacities={}
            for ident,client in current.get('clients',{}).items():
                before=previous.get('clients',{}).get(ident)
                if client['pci_id']!=pci or not before:continue
                for engine,value in client['engines'].items():
                    prior=before.get('engines',{}).get(engine)
                    if prior is None or value<prior:continue
                    engines[engine]=engines.get(engine,0)+value-prior
                    capacities[engine]=max(1,capacities.get(engine,1),client.get('capacity',{}).get(engine,1))
            if engines:
                row.update(utilization_percent=round(min(100,max(value/(elapsed*1e9*capacities[engine])*100 for engine,value in engines.items())),1),utilization_basis='Busiest GPU engine across visible DRM clients; other users may be excluded')
        if card.get('memory_total',0)>0 and 0<=card.get('memory_used',-1)<=card['memory_total']:
            row.update(memory_total_mib=card['memory_total']/2**20,memory_free_mib=(card['memory_total']-card['memory_used'])/2**20)
        results[pci]=row
    return results


class GpuSampler:
    def __init__(self,reader=None):
        self.reader=reader or (lambda target:capture(target,SCRIPT));self.previous=None

    def sample(self,target,hardware):
        try:
            current=self.reader(target);rows=activity(self.previous,current);self.previous=current
        except (OSError,ValueError,KeyError,TypeError,subprocess.SubprocessError):return hardware
        return {**hardware,'gpus':[{**gpu,**rows.get(gpu.get('pci_id'),{})} for gpu in hardware.get('gpus',[])]}
