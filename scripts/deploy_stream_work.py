"""Deploy streaming work through the standard atomic launcher update scheme.

Drain and verify one node at a time using the existing launcher deployer.
Existing Docker workers are preserved.
"""
import argparse
import json
from pathlib import Path
import sys
import urllib.parse

ROOT=(Path(__file__).resolve().parents[2] if Path(__file__).resolve().parents[1].name in ('GenomeHarbor', 'JSwarm') else Path(__file__).resolve().parents[1])
sys.path.insert(0,str(ROOT/'swarm-control/server'))
from agent_client import AgentClient,CONFIG
from agent_updates import LauncherUpdates,deployment_lock


def deploy(hosts,progress=print):
    with deployment_lock():
        return _deploy(hosts,progress)


def _deploy(hosts,progress):
    config=json.loads(CONFIG.read_text());updater=LauncherUpdates.desired(lambda:False,lambda *args:None);results=[]
    for host in hosts:
        if host['host'] not in config['peers']:continue
        host={**host,'lan_address':config['peers'][host['host']]}
        host['agent_endpoint']='http://'+host['lan_address']+':8790'
        client=AgentClient(host)
        try:
            status=client.call('status',timeout=8)
            if status['machine']!=host['machine']:raise RuntimeError('Enrolled machine identity changed')
            policy=status['worker_policy']
            value=updater.ensure(host,client,status,drain=True,wait_timeout=3600)
            if value.get('source_revision')!=updater.revision or not value.get('ready'):raise RuntimeError('Desired release is not ready')
            if policy.get('mode')!='draining' and not policy['accepting_jobs'] and value['worker_policy']['accepting_jobs']:raise RuntimeError('Previously paused worker unexpectedly accepting jobs')
            caps=client.call('stream.capabilities')
            if caps['version']!='launcher-stream-work.v1':raise RuntimeError('Streaming runtime unavailable after update')
            row={'host':host['host'],'status':'updated','revision':value['source_revision'],'accepting_jobs':value['worker_policy']['accepting_jobs']}
        except Exception as error:row={'host':host['host'],'status':'failed','error':str(error)}
        finally:client.close()
        results.append(row);progress(json.dumps(row))
        if row['status']=='failed':break
    return results


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--hosts',type=Path,default=Path('/mnt/PILEDRIVE/genomics-workbench/wgs/recall-hosts.json'))
    parser.add_argument('--host',action='append',help='Deploy selected enrolled host(s); default all enrolled hosts')
    args=parser.parse_args();hosts=json.loads(args.hosts.read_text())
    if args.host:hosts=[h for h in hosts if h['host'] in args.host]
    rows=deploy(hosts)
    raise SystemExit(1 if any(r['status']=='failed' for r in rows) else 0)


def manage(action,hosts_path):
    """Root run.py entry point; all changes use enrolled owner identities."""
    import concurrent.futures
    import subprocess
    config=json.loads(CONFIG.read_text())
    inventory={h['host']:h for h in json.loads(Path(hosts_path).read_text())}
    hosts=[{**inventory.get(name,{}),'host':name,'local':name==config['name'],
            'lan_address':ip,'agent_endpoint':'http://'+ip+':8790'} for name,ip in config['peers'].items()]
    updater=LauncherUpdates.desired(lambda:False,lambda *a:None)
    code="""import json,pathlib,time
p=pathlib.Path.home()/'.config/lifetech-launcher';policy=p/'worker-policy.json';marker=p/'maintenance.json'
if marker.exists():marker.replace(p/('maintenance-opened-'+str(time.time_ns())+'.json'))
temp=policy.with_suffix('.tmp');temp.write_text(json.dumps({'mode':'always'}));temp.chmod(0o600);temp.replace(policy)
"""
    def inspect(host):
        client=AgentClient(host)
        try:
            status=client.call('status',timeout=15)
            if host.get('machine') and host['machine']!=status['machine']:raise RuntimeError('Enrolled machine identity changed')
            host['machine']=status['machine']
            if action=='open':
                subprocess.run(updater.command(host,code),check=True,capture_output=True,text=True,timeout=25)
                status=client.call('status',timeout=15)
            return {'host':host['host'],'policy':status['worker_policy'],'stream':client.call('stream.capabilities',timeout=15)}
        except Exception as error:return {'host':host['host'],'error':str(error)}
        finally:client.close()
    if action=='deploy':
        # Standard update stages a busy node and never interrupts active workers.
        rows=deploy(hosts)
    else:
        with concurrent.futures.ThreadPoolExecutor(max_workers=max(1,len(hosts))) as pool:rows=list(pool.map(inspect,hosts))
        for row in rows:print(json.dumps(row),flush=True)
    return int(any('error' in row or row.get('status')=='failed' for row in rows))
