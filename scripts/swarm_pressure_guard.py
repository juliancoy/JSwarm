"""Drain new work under sustained storage pressure; preserve running jobs and owner policy."""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import time
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT),str(ROOT/'swarm-control/server')]
from agent_client import CONFIG
from agent_updates import LauncherUpdates


class PressureWindow:
    def __init__(self):self.hot=0;self.cool=0
    def observe(self, sample):
        disk=sample.get('local_disk_busy_percent')
        hot=sample['iowait_percent']>=30 and sample['io_pressure']>=50 and (disk is None or disk>=50)
        cool=sample['memory_pressure']<5 and ((disk is not None and disk<20) or
            (sample['iowait_percent']<15 and sample['io_pressure']<25))
        self.hot=self.hot+1 if hot else 0;self.cool=self.cool+1 if cool else 0
        if self.hot>=2:return 'drain'
        if self.cool>=3:return 'restore'
        return 'hold'


PROBE=r'''import hashlib,json,pathlib,sys,time
p=json.load(sys.stdin)
if hashlib.sha256(pathlib.Path('/etc/machine-id').read_bytes()).hexdigest()!=p['machine']:raise ValueError('Machine identity changed')
config=pathlib.Path.home()/'.config/lifetech-launcher/peers.json'
if json.loads(config.read_text())['name']!=p['name']:raise ValueError('Enrolled owner changed')
def counters():return list(map(int,pathlib.Path('/proc/stat').read_text().splitlines()[0].split()[1:9]))
def disks():
 names={d.name for d in pathlib.Path('/sys/block').iterdir() if '/virtual/' not in str(d.resolve())}
 return {v[2]:int(v[12]) for line in pathlib.Path('/proc/diskstats').read_text().splitlines() if (v:=line.split()) and v[2] in names}
a=counters();first=disks();started=time.monotonic();time.sleep(1);b=counters();last=disks();elapsed=time.monotonic()-started;v=[y-x for x,y in zip(a,b)]
busy=max((100*max(0,value-first[name])/(elapsed*1000) for name,value in last.items() if name in first),default=0)
def pressure(name):
 fields=pathlib.Path('/proc/pressure',name).read_text().splitlines()[0].split()[1:]
 return float(dict(x.split('=') for x in fields)['avg10'])
print(json.dumps({'iowait_percent':100*v[4]/max(1,sum(v)),'io_pressure':pressure('io'),'memory_pressure':pressure('memory'),'local_disk_busy_percent':min(100,busy),'sampled_at':time.time()}))
'''
UPDATE=r'''import hashlib,json,pathlib,sys,time
p=json.load(sys.stdin)
if hashlib.sha256(pathlib.Path('/etc/machine-id').read_bytes()).hexdigest()!=p['machine']:raise ValueError('Machine identity changed')
path=pathlib.Path.home()/'.config/lifetech-launcher/maintenance.json'
previous=json.loads(path.read_text()) if path.exists() else None
ours=isinstance(previous,dict) and previous.get('managed_by')=='swarm-pressure-guard'
if p['decision']=='drain' and (previous is None or ours):
 pending=path.with_suffix('.pressure.tmp');pending.write_text(json.dumps({'managed_by':'swarm-pressure-guard','reason':'Sustained disk I/O pressure; active work retained','sample':p['sample']}));pending.chmod(0o600);pending.replace(path)
if p['decision']=='restore' and ours:path.unlink()
print(json.dumps({'draining':path.exists(),'owner_maintenance':previous is not None and not ours}))
'''


def main():
    import os
    os.umask(0o077)
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--host',required=True);parser.add_argument('--once',action='store_true');args=parser.parse_args()
    guard=PressureWindow();updater=object.__new__(LauncherUpdates)
    folder=Path.home()/'.local/state/swarm-control';state=folder/('pressure-'+args.host+'.json')
    while True:
        try:
            cfg=json.loads(CONFIG.read_text());bulk=json.loads((Path.home()/'.local/state/lifetech-launcher/bulk-transport/config.json').read_text())
            address=cfg['peers'][args.host];host={'host':args.host,'local':args.host==cfg['name'],'lan_address':address}
            known=json.loads((folder/'enrolled-machines.json').read_text())
            payload={'name':args.host,'machine':bulk['peers'].get(args.host,{}).get('machine') or known[args.host]}
            result=subprocess.run(updater.command(host,PROBE),input=json.dumps(payload),capture_output=True,text=True,timeout=30,check=True)
            sample=json.loads(result.stdout);decision=guard.observe(sample)
            updated={}
            if decision!='hold':
                result=subprocess.run(updater.command(host,UPDATE),input=json.dumps({**payload,'decision':decision,'sample':sample}),capture_output=True,text=True,timeout=30,check=True)
                updated=json.loads(result.stdout)
            row={'host':args.host,'sample':sample,'decision':decision,**updated}
        except Exception as error:
            guard.hot=guard.cool=0
            row={'host':args.host,'error':type(error).__name__,'sampled_at':time.time()}
        pending=state.with_suffix('.tmp');pending.write_text(json.dumps(row));pending.chmod(0o600);pending.replace(state)
        print(json.dumps(row),flush=True)
        if args.once:return
        time.sleep(15)


if __name__=='__main__':main()
