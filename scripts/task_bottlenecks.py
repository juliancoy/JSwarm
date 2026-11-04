"""Read-only, bounded sampling of an alignment run's actual worker containers."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import subprocess
import time
from scripts import genomics_distributed as distributed
from scripts.genome_runtime import AgentClient


def load(path):
    try:return json.loads(Path(path).read_text())
    except (OSError,ValueError):return {}


def classify(row, now=None):
    now=time.time() if now is None else now
    if now-row.get('sampled_at',0)>45:return {'label':'Measurements stale','confidence':'unavailable','evidence':'Awaiting a fresh worker sample.'}
    if not row.get('running'):return {'label':'Outside alignment container','confidence':'unavailable','evidence':'Input transfer, GPU checks, or output verification may be active; no container CPU measurement yet.'}
    cpu=row.get('cpu_percent');reserved=row.get('reserved_cpus');metrics=row.get('host_metrics',{})
    evidence=[]
    if cpu is not None and reserved:evidence.append(f'{cpu/100:.2f} / {reserved:g} reserved CPU cores busy')
    if row.get('memory'):evidence.append('Container RAM '+row['memory'])
    if metrics.get('cpu_iowait_percent') is not None:evidence.append(f"host I/O wait {metrics['cpu_iowait_percent']:.1f}%")
    if metrics.get('disk_busy_percent') is not None:evidence.append(f"busiest host disk {metrics['disk_busy_percent']:.1f}% busy")
    if metrics.get('network_mbps') is not None:evidence.append(f"host network {metrics['network_mbps']:.1f} Mb/s (all workloads)")
    if (metrics.get('cpu_iowait_percent') or 0)>=10 and (metrics.get('disk_busy_percent') or 0)>=80:
        label='Possible storage contention';confidence='inferred'
    elif cpu is not None and reserved and cpu>=85*reserved:
        label='CPU allocation saturated';confidence='observed'
    elif cpu is not None and reserved and reserved>2 and 70<=cpu<180:
        label='Low CPU parallelism in current stage';confidence='observed'
    else:label='No dominant resource established';confidence='unavailable'
    return {'label':label,'confidence':confidence,'evidence':'; '.join(evidence) or 'Resource samples unavailable.','sampled_at':row['sampled_at']}


def sample(host, targets):
    client=AgentClient(host)
    try:
        status=client.call('status',timeout=10)
        if status.get('machine')!=host['machine']:raise ValueError('Worker identity changed')
        metrics=client.call('metrics',timeout=10)
    finally:client.close()
    result={}
    for ident,container,cpus in targets:
        p=subprocess.run(distributed.shell_command(host,['docker','stats','--no-stream','--format','{{json .}}',container]),capture_output=True,text=True,timeout=15)
        row={'sampled_at':time.time(),'host_metrics':{k:metrics.get(k) for k in ('percent','cpu_iowait_percent','disk_busy_percent','network_mbps')},'reserved_cpus':cpus,'running':False}
        if p.returncode==0 and p.stdout.strip():
            v=json.loads(p.stdout.splitlines()[-1]);row.update(running=True,cpu_percent=float(v['CPUPerc'].rstrip('%')),memory=v.get('MemUsage'),block_io=v.get('BlockIO'))
        result[ident]=row
    return result


def main(root):
    root=Path(root);hosts=[];discovered=0
    while True:
        try:
            if time.time()-discovered>300:hosts=distributed.discover(False);discovered=time.time()
            state=load(root/'state.json');targets={}
            bwa=load(root/'bwa-index.json')
            if bwa.get('phase')=='running' and not (root/'reference/bwa-verified.json').exists():targets.setdefault(bwa['host'],[]).append(('compact-reference',bwa['container'],bwa['cpus']))
            for j in state.get('jobs',{}).values():
                if j.get('phase')!='running':continue
                host=j.get('host');p=root/'attempts'/(host+'-'+j['id']+'.json')
                report=load(p) or load(root/'attempts'/(host.replace('.','_')+'-'+j['id']+'.json'))
                if report.get('phase')=='running':targets.setdefault(host,[]).append(('alignment:'+host,report['container'],report['cpus']))
            values={};errors=[]
            with ThreadPoolExecutor(max_workers=5) as pool:
                pending=[(h['host'],pool.submit(sample,h,targets[h['host']])) for h in hosts if h.get('eligible') and h['host'] in targets]
                for host,f in pending:
                    try:values.update(f.result())
                    except Exception:errors.append(host+': resource sampling unavailable')
            out={'sampled_at':time.time(),'samples':values,'errors':errors}
            path=root/'bottleneck-samples.json';tmp=path.with_suffix('.tmp');tmp.write_text(json.dumps(out));tmp.chmod(0o600);tmp.replace(path)
        except Exception:pass
        time.sleep(10)


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--root',required=True);main(p.parse_args().root)
