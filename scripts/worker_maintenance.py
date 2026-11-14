"""Inspect, drain, or resume enrolled workers without interrupting running jobs.

Drain closes admission while existing signed lease renewals remain available.
Stopping persistent producers and applying updates are separate owner actions.
"""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import time

ROOT=(Path(__file__).resolve().parents[2] if Path(__file__).resolve().parents[1].name in ('HarborGenetics', 'JSwarm') else Path(__file__).resolve().parents[1])
sys.path.insert(0,str(ROOT/'swarm-control/server'))
from agent_client import AgentClient,CONFIG
from agent_updates import LauncherUpdates

CHANGE=r'''import json,pathlib,sys,time
p=json.load(sys.stdin);config=pathlib.Path.home()/'.config/lifetech-launcher'
if json.loads((config/'peers.json').read_text())['name']!=p['node']:raise ValueError('Enrolled owner identity changed')
marker=config/'maintenance.json';policy=config/'worker-policy.json'
if p['action']=='drain':
 if not marker.exists():
  value={'started':time.time(),'reason':p['reason'],'phase':'draining','previous_policy':json.loads(policy.read_text()) if policy.exists() else {'mode':'auto'}}
  temp=marker.with_suffix('.tmp');temp.write_text(json.dumps(value));temp.chmod(0o600);temp.replace(marker)
elif p['action']=='resume' and marker.exists():
 value=json.loads(marker.read_text());previous=value.get('previous_policy',{'mode':'auto'})
 if previous.get('mode') not in ('auto','always','paused'):raise ValueError('Invalid saved worker preference')
 temp=policy.with_suffix('.tmp');temp.write_text(json.dumps(previous));temp.chmod(0o600);temp.replace(policy)
 archived=config/('maintenance-completed-'+str(time.time_ns())+'.json');marker.replace(archived)
print(json.dumps({'node':p['node'],'maintenance':marker.exists()}))
'''


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=('status','drain','resume'),nargs='?',default='status')
    parser.add_argument('--node',action='append',help='An enrolled node; default is all current nodes')
    parser.add_argument('--reason',default='Owner-requested maintenance')
    args=parser.parse_args()
    config=json.loads(CONFIG.read_text());updater=LauncherUpdates.desired(lambda:False,lambda *a:None)
    nodes=args.node or list(config['peers'])
    if any(n not in config['peers'] for n in nodes):parser.error('Use an enrolled node name')
    hosts=[{'host':n,'local':n==config['name'],'lan_address':config['peers'][n],
            'agent_endpoint':'http://'+config['peers'][n]+':8790'} for n in nodes]
    # Validate the whole fleet before reopening any admission gates.
    if args.action=='resume':
        for host in hosts:
            client=AgentClient(host)
            try:
                status=client.call('status')
                if not status.get('ready') or status.get('source_revision')!=updater.revision:
                    raise RuntimeError('Updated worker is not ready: '+host['host'])
            finally:client.close()
    for host in hosts:
        if args.action!='status':
            result=subprocess.run(updater.command(host,CHANGE),input=json.dumps({'node':host['host'],'action':args.action,'reason':args.reason}),capture_output=True,text=True,timeout=20)
            if result.returncode:raise RuntimeError('Maintenance change failed on '+host['host']+': '+result.stderr[-200:])
        client=AgentClient(host)
        try:
            status=client.call('status')
            print(json.dumps({'node':host['host'],'ready':status.get('ready'),'revision':status.get('source_revision'),
                              'policy':status.get('worker_policy'),'update':status.get('update'),
                              'active_jobs':len(status.get('active_jobs',[])),'active_leases':status.get('active_resource_leases',0)}),flush=True)
        finally:client.close()


if __name__=='__main__':main()
