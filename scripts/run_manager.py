"""Run-manager input planning and authenticated concurrent sequential QC inputs.

`plan` is read-only. `start` delegates only queued BAM read-QC inputs to this
compute node, keeps running jobs intact, and uses independent mTLS input serving.
The original run retains its completed receipts and displays delegated children.
"""
import argparse
import base64
import importlib.util
import json
import hashlib
from pathlib import Path
import shutil
import subprocess
import sys
import time

from scripts.run_inputs import plan_inputs

ROOT=(Path(__file__).resolve().parents[2] if Path(__file__).resolve().parents[1].name in ('GenomeHarbor', 'JSwarm') else Path(__file__).resolve().parents[1])


def worker(root):
    spec=importlib.util.spec_from_file_location('run_manager_peer',root/'worker.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module


def source_host(name):
    sys.path.insert(0,str(ROOT/'swarm-control/server'))
    from docker_utils.policy import inventory,ssh
    h=next(h for h in inventory() if h['host']==name)
    return ssh(h)


INSTALL_SOURCE=r'''import base64,json,pathlib,subprocess,sys
p=json.load(sys.stdin);r=pathlib.Path(p['root']).resolve();r.relative_to(pathlib.Path.home()/'.local/state/venter-peer-qc')
import hashlib,os
config=pathlib.Path(os.environ.get('LIFETECH_PEER_CONFIG',str(pathlib.Path.home()/'.config/lifetech-launcher/peers.json')))
runtime=(config.parent/'input-releases/current/scripts').resolve()
for name in ['run_input_source.py','run_input_cache.py']:
 if not (runtime/name).is_file() or hashlib.sha256((runtime/name).read_bytes()).digest()!=hashlib.sha256(base64.b64decode(p[name])).digest():
  raise RuntimeError('Input runtime update required through the standard machine deployer')
path=r/p.get('delegation_file','stream-delegation.json');temp=path.with_suffix('.install')
temp.write_bytes(base64.b64decode(p['stream-delegation.json']));temp.chmod(0o600);temp.replace(path)
unit=pathlib.Path.home()/'.config/systemd/user'/p['unit']
unit.write_text('[Unit]\nDescription=Independent authenticated input streams for run manager\nStartLimitIntervalSec=0\n[Service]\nExecStart=/usr/bin/python3 '+str(runtime/'run_input_source.py')+' --root '+str(r)+' --delegation '+str(r/p.get('delegation_file','stream-delegation.json'))+(' --cache-only' if p.get('cache_only') else '')+'\nUMask=0077\nRestart=on-failure\nRestartSec=30\n[Install]\nWantedBy=default.target\n')
subprocess.run(['systemctl','--user','daemon-reload'],check=True)
subprocess.run(['systemctl','--user','enable','--now',p['unit']],check=True)
'''

DRAIN_SOURCE=r'''import importlib.util,json,os,pathlib,signal,subprocess,sys,time
p=json.load(sys.stdin);root=pathlib.Path(p['root']).resolve();spec=importlib.util.spec_from_file_location('drain_peer',root/'worker.py');w=importlib.util.module_from_spec(spec);spec.loader.exec_module(w)
authority=w.load(pathlib.Path.home()/'.config/lifetech-launcher/peers.json')['controller_public_key'];m=w.check_manifest(p['manifest'],authority)
if m['id']!=w.load(root/'manifest.json')['value']['id']:raise ValueError('Different run')
ids=subprocess.check_output(['docker','ps','-q','--filter','label=org.lifetech.venter-peer='+m['id']],text=True,timeout=10).split()
if ids:print('busy');sys.exit(0)
pid=int(subprocess.check_output(['systemctl','--user','show','venter-peer-qc.service','-p','MainPID','--value'],text=True))
if pid<=1 or str(root/'worker.py').encode() not in (pathlib.Path('/proc')/str(pid)/'cmdline').read_bytes().split(b'\0'):raise ValueError('Unexpected peer process')
os.kill(pid,signal.SIGSTOP)
try:
 ids=subprocess.check_output(['docker','ps','-q','--filter','label=org.lifetech.venter-peer='+m['id']],text=True,timeout=10).split()
 if ids:print('busy');sys.exit(0)
 w.atomic(root/('manifest-before-stream-placement-'+str(time.time_ns())+'.json'),w.load(root/'manifest.json'))
 w.atomic(root/'manifest.json',p['manifest']);os.kill(pid,signal.SIGTERM)
finally:os.kill(pid,signal.SIGCONT)
subprocess.run(['systemctl','--user','restart','venter-peer-qc.service'],check=True,timeout=90)
print('migrated')
'''


def sign(value,w):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    key=Ed25519PrivateKey.from_private_bytes((Path.home()/'.config/map-master/controller-signing.key').read_bytes())
    return w.signed(value,key)


def plan_for_peer(peer,jobs,mode,parallelism):
    spec=importlib.util.spec_from_file_location('run_input_lease',peer.folder/'lease.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    capacity=module.ResourceLease.pool_capacity(directory=peer.config['resource_directory'],group='venter-stream-qc')
    return plan_inputs(jobs,mode,parallelism,shutil.disk_usage(peer.folder).free,
                       capacity['cpus'],capacity['memory_mb'])


def verified_delegation(w,peer,path):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    public=w.load(Path.home()/'.config/lifetech-launcher/peers.json')['controller_public_key']
    envelope=w.load(path);value=w.verified(envelope,Ed25519PublicKey.from_public_bytes(bytes.fromhex(public)))
    if value['parent_run']!=peer.manifest['id'] or value['worker']!=peer.name or not value['created']<=time.time()<=value['expires']:
        raise ValueError('Delegation identity or authorization window changed')
    for job in value['jobs']:
        if job['spec']['kind']!='bam-read-qc' or hashlib.sha256(w.canonical(job['spec'])).hexdigest()!=job['id']:
            raise ValueError('Delegated input identity changed')
    return envelope


def configure(root,mode,parallelism,replace_endpoint=False):
    w=worker(root);peer=w.Peer(root);peer.poll();m=peer.manifest
    old=verified_delegation(w,peer,root/'stream-delegation.json') if (root/'stream-delegation.json').exists() else None
    if old and not replace_endpoint:
        print(json.dumps({'existing_run':old['value']['id'],'status':'already delegated; existing work preserved'}));return
    if old:
        jobs=old['value']['jobs'];ident=old['value']['id']
    else:
        busy={r.get('current_job') for r in peer.peers.values()}|{peer.current}
        jobs=[j for j in m['jobs'] if j['id'] not in peer.receipts and j['id'] not in busy and j['spec']['kind']=='bam-read-qc']
        ident='venter-stream-'+peer.name.replace('.local','')+'-'+str(int(time.time()))
    if not old and w.load(root/'state.json').get('current_job'):
        raise RuntimeError('Compute peer is active; retry start once idle; no placement changed')
    plan=plan_for_peer(peer,jobs,mode,parallelism)
    if plan.mode!='stream':raise ValueError('Cached jobs remain with the existing native peer executor')
    sources={j.get('source_worker',j['preferred_worker']) for j in jobs}
    if len(sources)!=1:raise ValueError('Create separate input groups for separate source peers')
    source=next(iter(sources));source_node=m['nodes'][source]
    suffix=hashlib.sha256(ident.encode()).hexdigest()[:12]
    executor_unit=old['value'].get('executor_unit','venter-stream-offload.service') if old else 'run-manager-stream-'+suffix+'.service'
    source_port=old['value'].get('source_port',8797) if old else 8800+int(suffix[:4],16)%20000
    d={'schema':'venter-stream-offload.v1','id':ident,'parent_run':m['id'],'worker':peer.name,'source':source,
       'module_sha256':w.digest(ROOT/'scripts/venter_stream_offload.py'),'source_module_sha256':w.digest(ROOT/'scripts/run_input_source.py'),
       'created':time.time()-5,'expires':m['expires'],'jobs':jobs,'parallel_streams':plan.parallelism,
       'source_address':source_node['address'],'source_port':source_port,'input_plan':plan.as_dict(),'executor_unit':executor_unit,
       'cache_peers':{name:{'address':node['address'],'port':30000+int(hashlib.sha256((ident+name).encode()).hexdigest()[:4],16)%20000} for name,node in m['nodes'].items() if name!=source},
       'peer_selector_sha256':w.digest(ROOT/'scripts/run_cache_peers.py'),
       'cache_module_sha256':w.digest(ROOT/'scripts/run_input_cache.py'),
       'shared_input_cache':{'disk_bytes':2*1024**3,'block_bytes':4*1024**2},
       'transport':'Pinned mTLS 1.3; ordinary NIC DMA, no RDMA or zero-copy claim'}
    envelope=sign(d,w)
    payload={'root':source_node['root'],'unit':'run-input-source-'+suffix+'.service','run_input_source.py':base64.b64encode((ROOT/'scripts/run_input_source.py').read_bytes()).decode(),
             'run_input_cache.py':base64.b64encode((ROOT/'scripts/run_input_cache.py').read_bytes()).decode(),
             'stream-delegation.json':base64.b64encode(w.canonical(envelope)).decode()}
    command=source_host(source)+[__import__('shlex').join(['python3','-c',INSTALL_SOURCE])]
    subprocess.run(command,input=w.canonical(payload),check=True,timeout=90)
    for name in d['cache_peers']:
        participant={**payload,'root':m['nodes'][name]['root'],'unit':'run-input-cache-'+suffix+'.service','cache_only':True,'delegation_file':'cache-delegation-'+suffix+'.json'}
        if name==peer.name:
            subprocess.run([sys.executable,'-c',INSTALL_SOURCE],input=w.canonical(participant),check=True,timeout=90)
        else:
            command=source_host(name)+[__import__('shlex').join(['python3','-c',INSTALL_SOURCE])]
            try:subprocess.run(command,input=w.canonical(participant),check=True,timeout=90)
            except subprocess.SubprocessError:print(json.dumps({'cache_peer':name,'status':'unavailable; origin fallback remains enabled'}))
    # Source serving is independent of its current compute executor; no active
    # source scan or conversation service is restarted.
    if old:subprocess.run(['systemctl','--user','stop',executor_unit],check=True)
    if not old:
        pending={j['id'] for j in jobs};updated={**m,'jobs':[j for j in m['jobs'] if j['id'] not in pending],
            'delegated_jobs':m.get('delegated_jobs',[])+[{'job':j['id'],'run':ident,'worker':peer.name,'mode':'streamed QC'} for j in jobs]}
        state=w.load(root/'state.json')
        if state.get('current_job'):raise ValueError('Compute peer is active; defer placement migration until idle')
        subprocess.run(['systemctl','--user','stop','venter-peer-qc.service'],check=True)
        w.atomic(root/'manifest-before-stream-placement.json',w.load(root/'manifest.json'))
        w.atomic(root/'manifest.json',sign(updated,w));subprocess.run(['systemctl','--user','start','venter-peer-qc.service'],check=True)
    w.atomic(root/'stream-delegation.json',envelope)
    registry=Path.home()/'.local/state/swarm-control/tasks'/(ident+'.json')
    units=Path.home()/'.config/systemd/user'
    unit=units/executor_unit
    unit.write_text('[Unit]\nDescription=Run-manager concurrent streamed BAM QC\nStartLimitIntervalSec=0\n[Service]\nWorkingDirectory='+str(ROOT)+'\nExecStart=/usr/bin/python3 -m scripts.venter_stream_offload --root '+str(root)+' --delegation '+str(root/'stream-delegation.json')+' --registry '+str(registry)+'\nUMask=0077\nRestart=on-failure\nRestartSec=60\nTimeoutStopSec=90\n[Install]\nWantedBy=default.target\n')
    drain=units/('run-manager-placement-'+suffix+'.service')
    drain.write_text('[Unit]\nDescription=Reconcile streamed input placement without interrupting active QC\n[Service]\nWorkingDirectory='+str(ROOT)+'\nExecStart=/usr/bin/python3 -m scripts.run_manager reconcile --root '+str(root)+'\nRestart=on-failure\nRestartSec=60\n[Install]\nWantedBy=default.target\n')
    subprocess.run(['systemctl','--user','daemon-reload'],check=True)
    subprocess.run(['systemctl','--user','enable','--now',unit.name,drain.name],check=True)
    print(json.dumps({'run':ident,'plan':plan.as_dict(),'source_scan':'preserved','input_cache_required':False}))


def reconcile(root):
    w=worker(root);peer=w.Peer(root);auth=verified_delegation(w,peer,root/'stream-delegation.json')['value'];m=dict(peer.manifest)
    # Preserve source availability for streamed objects, but remove the local-
    # only scheduling restriction after the source's existing scan has drained.
    jobs={j['id']:j for j in m['jobs']}
    for job in auth['jobs']:jobs[job['id']]={**job,'preferred_worker':auth['worker'],'data_local_only':True}
    m['jobs']=list(jobs.values());envelope=sign(m,w);source=auth['source'];source_root=m['nodes'][source]['root']
    while time.time()<auth['expires']:
        try:
            value=w.verified(peer.remote_json(source,'/status'),peer.keys[source])
            if value.get('current_job') or abs(time.time()-value['time'])>60:time.sleep(5);continue
            payload={'root':source_root,'manifest':envelope}
            command=source_host(source)+[__import__('shlex').join(['python3','-c',DRAIN_SOURCE])]
            result=subprocess.run(command,input=w.canonical(payload),capture_output=True,timeout=120)
            if result.returncode==0 and b'migrated' in result.stdout:
                w.atomic(root/'stream-placement-reconciled.json',{'source':source,'time':time.time(),'run':auth['id']});return
        except (OSError,subprocess.SubprocessError,ValueError):pass
        time.sleep(5)
    raise RuntimeError('Source placement authorization expired before idle migration')


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('action',choices=['plan','start','reconcile'])
    parser.add_argument('--root',type=Path,required=True);parser.add_argument('--mode',choices=['auto','stream','cache'],default='auto')
    parser.add_argument('--parallelism',type=int,default=2);parser.add_argument('--replace-endpoint',action='store_true')
    args=parser.parse_args()
    if args.action=='reconcile':reconcile(args.root)
    elif args.action=='start':configure(args.root,args.mode,args.parallelism,args.replace_endpoint)
    else:
        w=worker(args.root);peer=w.Peer(args.root);peer.poll();busy={r.get('current_job') for r in peer.peers.values()}
        jobs=[j for j in peer.manifest['jobs'] if j['id'] not in peer.receipts and j['id'] not in busy and j['spec']['kind']=='bam-read-qc']
        if not jobs and (args.root/'stream-delegation.json').exists():jobs=verified_delegation(w,peer,args.root/'stream-delegation.json')['value']['jobs']
        print(json.dumps(plan_for_peer(peer,jobs,args.mode,args.parallelism).as_dict()))

if __name__=='__main__':main()
