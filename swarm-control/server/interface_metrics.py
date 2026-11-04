"""Read-only physical interface counters via enrolled hosts and saved SSH access."""
import json
from pathlib import Path
import subprocess
import sys

SCRIPT = r'''import json,time,re,subprocess
from pathlib import Path
interfaces={}
for device in sorted(Path('/sys/class/net').iterdir()):
 if device.name=='lo' or '/virtual/' in str(device.resolve()):continue
 if len(interfaces)>=32:break
 try:
  interfaces[device.name]={'ifindex':int((device/'ifindex').read_text()),'state':(device/'operstate').read_text().strip(),
   'rx_bytes':int((device/'statistics/rx_bytes').read_text()),'tx_bytes':int((device/'statistics/tx_bytes').read_text())}
 except (OSError,ValueError):continue
 row=interfaces[device.name]
 for field in ('rx_errors','tx_errors','rx_dropped','tx_dropped','rx_crc_errors'):
  try:row[field]=int((device/'statistics'/field).read_text())
  except (OSError,ValueError):pass
 for field in ('carrier_changes','carrier_down_count','carrier_up_count'):
  try:row[field]=int((device/field).read_text())
  except (OSError,ValueError):pass
 try:
  speed=int((device/'speed').read_text())
  if speed>0:row['link_speed_mbps']=speed
 except (OSError,ValueError):pass
 try:
  duplex=(device/'duplex').read_text().strip()
  if duplex in ('full','half'):row['link_duplex']=duplex
 except OSError:pass
 if (device/'wireless').exists() or (device/'phy80211').exists():
  row.pop('link_speed_mbps',None);row.pop('link_duplex',None)
  try:
   result=subprocess.run(['iw','dev',device.name,'link'],capture_output=True,text=True,timeout=.5)
   for direction,rate in re.findall(r'(rx|tx) bitrate:\s*([0-9.]+) MBit/s',result.stdout):
    value=float(rate)
    if value>0:row['link_'+direction+'_mbps']=value
  except (OSError,ValueError,subprocess.SubprocessError):pass
print(json.dumps({'monotonic':time.monotonic(),'sampled_at':time.time(),
 'boot_id':Path('/proc/sys/kernel/random/boot_id').read_text().strip(),'interfaces':interfaces}))
'''


def capture(target,script=SCRIPT,timeout=5):
    if target.get('local'):
        command=[sys.executable,'-c',script];payload=None
    else:
        from agent_client import CONFIG
        config=json.loads(CONFIG.read_text())
        address=config['peers'][target['host']]
        inventory=Path.home()/'.local/state/orgportal/lan-ssh-devices.json'
        accounts=json.loads(inventory.read_text()).get('accounts',{}) if inventory.exists() else {}
        alias=target['host'] if target['host'].endswith('.local') else target['host']+'.local'
        command=['ssh','-o','AddressFamily=inet','-o','BatchMode=yes','-o','StrictHostKeyChecking=yes','-o','ConnectTimeout=3',
                 '-o','ForwardAgent=no','-o','HostName='+address,'-o','HostKeyAlias='+alias]
        key=Path.home()/'.ssh/orgportal_lan_ed25519'
        if key.exists():command+=['-i',str(key)]
        if accounts.get(alias):command+=['-l',accounts[alias]]
        command += [alias,'python3 -'];payload=script
    result=subprocess.run(command,input=payload,capture_output=True,text=True,timeout=timeout,check=True)
    if len(result.stdout)>65536:raise ValueError('Interface response exceeds limit')
    return json.loads(result.stdout)


class InterfaceSampler:
    def __init__(self,reader=capture):
        self.reader=reader;self.previous=None;self.seen={}

    def sample(self,target):
        try:current=self.reader(target)
        except (OSError,ValueError,subprocess.SubprocessError):return []
        previous=self.previous;self.previous=current
        elapsed=current['monotonic']-previous['monotonic'] if previous else 0
        valid=previous and previous.get('boot_id')==current.get('boot_id') and 0<elapsed<=30
        rows=[]
        for name,counters in list(current.get('interfaces',{}).items())[:32]:
            row={'name':name,'ifindex':counters.get('ifindex'),'state':counters.get('state'),
                 'status':'warming','sampled_at':current['sampled_at'],'window_seconds':elapsed if valid else None,
                 'rx_mbps':None,'tx_mbps':None,'network_mbps':None}
            for field in ('link_speed_mbps','link_rx_mbps','link_tx_mbps'):
                value=counters.get(field)
                if isinstance(value,(int,float)) and not isinstance(value,bool) and 0<value<1000000:row[field]=value
            if counters.get('link_duplex') in ('full','half'):row['link_duplex']=counters['link_duplex']
            before=(previous.get('interfaces') or {}).get(name) if previous else None
            if valid and before and before.get('ifindex')==counters.get('ifindex'):
                changes=[counters[key]-before[key] for key in ('rx_bytes','tx_bytes')]
                if min(changes)>=0:
                    rx,tx=[round(change*8/elapsed/1000000,4) for change in changes]
                    row.update(status='live',rx_mbps=rx,tx_mbps=tx,network_mbps=round(rx+tx,4))
            # Counter deltas, including zero, require a continuous sample.
            for field in ('rx_errors','tx_errors','rx_dropped','tx_dropped','rx_crc_errors','carrier_changes','carrier_down_count','carrier_up_count'):
                value=counters.get(field)
                if type(value) is int and value>=0:row[field+'_total']=value
                older=before.get(field) if before else None
                row[field+'_delta']=value-older if valid and before and before.get('ifindex')==counters.get('ifindex') and type(value) is int and type(older) is int and value>=older>=0 else None
            previous_seen=self.seen.get(name,{})
            row['last_link_change_at']=previous_seen.get('last_link_change_at') if valid else None
            if valid and before and (row.get('carrier_changes_delta',0) or before.get('state')!=counters.get('state')):
                row['last_link_change_at']=current['sampled_at']
            speed=row.get('link_speed_mbps')
            for direction in ('rx','tx'):
                rate=row.get(direction+'_mbps')
                row[direction+'_utilization_percent']=round(rate*100/speed,2) if speed and rate is not None else None
            if row['state'] in ('down','lowerlayerdown','notpresent'):
                row.update(status='disconnected',rx_mbps=None,tx_mbps=None,network_mbps=None,rx_utilization_percent=None,tx_utilization_percent=None)
            rows.append(row)
            self.seen[name]=dict(row)
        present={row['name'] for row in rows}
        for name,row in self.seen.items():
            if name not in present and len(rows)<32:
                absent={**row,'state':'notpresent','status':'disconnected','sampled_at':current['sampled_at'],
                        'rx_mbps':None,'tx_mbps':None,'network_mbps':None,'rx_utilization_percent':None,'tx_utilization_percent':None}
                for field in list(absent):
                    if field.endswith('_delta'):absent[field]=None
                rows.append(absent)
        self.seen={row['name']:dict(row) for row in rows}
        return rows
