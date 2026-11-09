"""On-demand machine inspector through the enrolled JSwarm protocol only.

Fixed read-only diagnostics, one bounded RAM transfer test per ten minutes and
per-node single-flight caching. Never returns credentials or arbitrary commands.
"""
import base64
import datetime
import hashlib
import json
import math
import secrets
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from agent_client import AgentClient, CONFIG

POOL=ThreadPoolExecutor(max_workers=3,thread_name_prefix='machine-inspector')
LOCK=threading.Lock()
CACHE={}
PROBE_BYTES=4*1024**2


def command(client,argv,payload=b'',limit=8*1024**2,timeout=30):
    ident=secrets.token_hex(16);offsets={'stdout':0,'stderr':0};sent=0;credit=512*1024
    output=bytearray();errors=bytearray();deadline=time.monotonic()+timeout
    client.call('command.start',{'id':ident,'argv':argv},timeout=5)
    started=time.monotonic()
    try:
        while time.monotonic()<deadline:
            chunk=payload[sent:sent+min(credit,256*1024)]
            row=client.call('command.poll',{'id':ident,**offsets,'offset':sent,
                'data':base64.b64encode(chunk).decode(),'eof':sent+len(chunk)==len(payload)},timeout=5)
            if row['input_offset']!=sent+len(chunk):raise ValueError('Input acknowledgement differs')
            sent=row['input_offset'];credit=row['input_credit'];received=False
            for name,target in (('stdout',output),('stderr',errors)):
                if row[name+'_offset']!=offsets[name]:raise ValueError('Output acknowledgement differs')
                block=base64.b64decode(row[name],validate=True)
                if len(target)+len(block)>limit:raise ValueError('Diagnostic output exceeds limit')
                target.extend(block);offsets[name]+=len(block);received|=bool(block)
            if row['exit_code'] is not None and row['stdout_eof'] and row['stderr_eof']:
                return bytes(output),row['exit_code'],time.monotonic()-started
            if not chunk and not received:time.sleep(.005)
        raise TimeoutError('JSwarm diagnostic timed out')
    finally:
        try:client.call('command.close',{'id':ident},timeout=3)
        except Exception:pass


def finite(value):
    return type(value) in (int,float) and math.isfinite(value) and value>=0


def capacity(available,total):
    if not finite(available) or not finite(total) or total<=0 or available>total:return None
    return {'available_bytes':available,'total_bytes':total,'available_percent':round(100*available/total,1)}


def project(name,status,metrics,runtime=None):
    memory=metrics.get('performance',{}).get('memory',{})
    total=memory.get('total_bytes',metrics.get('ram_total_bytes'))
    available=memory.get('available_bytes')
    if available is None and finite(total) and finite(metrics.get('ram_used_bytes')):available=total-metrics['ram_used_bytes']
    hardware=metrics.get('hardware') or status.get('hardware') or {}
    disks=[];seen=set()
    for fs in metrics.get('performance',{}).get('filesystems',[]):
        key=(fs.get('device'),fs.get('total_bytes'))
        if key in seen:continue
        c=capacity(fs.get('available_bytes'),fs.get('total_bytes'))
        if c:
            seen.add(key);disks.append({'name':fs.get('device') or fs.get('mount'),'path':fs.get('mount'),**c})
    if not disks:
        storage=hardware.get('storage') or {};c=capacity(storage.get('free_bytes'),storage.get('total_bytes'))
        if c:disks.append({'name':'JSwarm storage','path':storage.get('path'),**c})
    ram=capacity(available,total)
    gpus=[]
    for gpu in hardware.get('gpus',[]):
        busy=gpu.get('utilization_percent');total=gpu.get('memory_total_mib');free=gpu.get('memory_free_mib')
        gpus.append({**{key:gpu.get(key) for key in ('uuid','pci_id','vendor','backend')},'name':gpu.get('name') or gpu.get('vendor') or 'GPU',
                     'available_percent':round(100-busy,1) if finite(busy) and busy<=100 else None,
                     'basis':gpu.get('utilization_basis') or 'Reported GPU engine utilization',
                     'memory':capacity(free*2**20,total*2**20) if finite(free) and finite(total) else None})
    return {'host':name,'status':'live' if metrics.get('status')=='live' else 'stale',
            'sampled_at':metrics.get('sampled_at'),'fetched_at':time.time(),
            'ram':ram,'disks':disks,'gpus':gpus,
            'node_revision':status.get('source_revision'),'commit':(runtime or {}).get('commit'),
            'started_at':(runtime or {}).get('started_at'),'uptime_seconds':(runtime or {}).get('uptime_seconds'),
            'node_container':(runtime or {}).get('name'),'policy':status.get('worker_policy',{}).get('mode'),
            'source':'Selected machine’s authenticated JSwarm node'}


CPU_SAMPLE = r"""
import json,time
from pathlib import Path
def read():
    return {parts[0]:list(map(int,parts[1:9])) for line in Path('/proc/stat').read_text().splitlines() if (parts:=line.split()) and parts[0].startswith('cpu') and parts[0][3:].isdigit()}
a=read();time.sleep(.3);b=read();cores=[]
for name,values in b.items():
    if name not in a:continue
    delta=[max(0,y-x) for x,y in zip(a[name],values)];total=sum(delta)
    if not total:continue
    user=100*(delta[0]+delta[1])/total
    system=100*(delta[2]+delta[5]+delta[6])/total
    wait=100*delta[4]/total;steal=100*delta[7]/total
    cores.append({'id':int(name[3:]),'user_percent':user,'system_percent':system,'iowait_percent':wait,'steal_percent':steal,'busy_percent':user+system+steal})
print(json.dumps({'cores':cores,'sampled_at':time.time(),'interval_seconds':.3}))
"""


def cpu_usage(client):
    output,code,_=command(client,['python3','-c',CPU_SAMPLE],limit=256*1024)
    if code:raise ValueError('CPU sampling unavailable')
    value=json.loads(output)
    if not isinstance(value.get('cores'),list):raise ValueError('Invalid CPU sample')
    return value


def node_runtime(client):
    # docker itself executes inside that machine's enrolled JSwarm command service.
    fmt='{{json .Name}}\t{{json .State.Running}}\t{{json .State.StartedAt}}\t{{json .Config.Labels}}'
    names=['swarm-node','organization-test-agent','lifetech-launcher','lifetech-launcher-session']
    data,_,_=command(client,['docker','inspect','--format',fmt,*names],limit=128*1024)
    if not data.strip():
        try:data,_,_=command(client,['docker','--host','unix:///var/run/docker.sock','inspect','--format',fmt,*names],limit=128*1024)
        except Exception:return {}
    for line in data.decode().splitlines():
        try:
            name,running,start,labels=map(json.loads,line.split('\t'))
            if not running:continue
            commit=(labels or {}).get('org.opencontainers.image.revision') or (labels or {}).get('org.lifetech.commit')
            if not isinstance(commit,str) or len(commit) not in (40,64) or any(c not in '0123456789abcdef' for c in commit):commit=None
            at=datetime.datetime.fromisoformat(start.replace('Z','+00:00')).timestamp()
            return {'name':name.lstrip('/'),'started_at':start,'uptime_seconds':max(0,time.time()-at),'commit':commit}
        except (ValueError,TypeError):continue
    return {}


def probe(client,source,target):
    if source==target:return {'status':'local','source':source,'target':target,'note':'Local coordinator; no LAN path to test.'}
    block=bytes(range(256))*(PROBE_BYTES//256);digest=hashlib.sha256(block).hexdigest()
    upload="import sys,hashlib,json; b=sys.stdin.buffer.read("+str(PROBE_BYTES+1)+"); print(json.dumps({'bytes':len(b),'sha256':hashlib.sha256(b).hexdigest()}))"
    out,code,send_seconds=command(client,['python3','-c',upload],block)
    ack=json.loads(out)
    if code or ack!={'bytes':PROBE_BYTES,'sha256':digest}:raise ValueError('Upload test verification failed')
    download='import sys; sys.stdout.buffer.write(bytes(range(256))*'+str(PROBE_BYTES//256)+'); sys.stdout.buffer.flush()'
    out,code,receive_seconds=command(client,['python3','-c',download])
    if code or len(out)!=PROBE_BYTES or hashlib.sha256(out).hexdigest()!=digest:raise ValueError('Download test verification failed')
    return {'status':'tested','source':source,'target':target,'sampled_at':time.time(),
            'to_node_mbps':PROBE_BYTES*8/send_seconds/1e6,'from_node_mbps':PROBE_BYTES*8/receive_seconds/1e6,
            'bytes_each_direction':PROBE_BYTES,'verified':True,
            'scope':'4 MiB each direction, RAM only; authenticated JSwarm command path, including RPC/encoding overhead. Sample under current load, not raw Ethernet capacity or browser-to-node speed.'}


def collect(host,source,previous):
    client=AgentClient(host);name=host['host'];value={}
    try:
        status=client.call('status',timeout=4)
        if status.get('node')!=name:raise ValueError('JSwarm node identity differs')
        metrics=client.call('metrics',timeout=4)
        runtime=previous.get('_runtime') or {}
        runtime_at=previous.get('_runtime_at',0)
        if time.time()-runtime_at>=60:
            try:runtime=node_runtime(client);runtime_at=time.time()
            except Exception:pass
        if runtime.get('started_at'):
            try:runtime={**runtime,'uptime_seconds':max(0,time.time()-datetime.datetime.fromisoformat(runtime['started_at'].replace('Z','+00:00')).timestamp())}
            except (ValueError,TypeError):pass
        value=project(name,status,metrics,runtime)
        value.update(_runtime=runtime,_runtime_at=runtime_at)
        try:value['cpu']=cpu_usage(client)
        except Exception:value['cpu']=None
        speed=previous.get('speed') or {}
        if speed.get('status') in ('tested','local') and time.time()-speed.get('sampled_at',time.time())<600:
            value['speed']=speed
        else:
            # Publish resources immediately; the test must not delay the inspector.
            value['speed']={'status':'testing','source':source,'target':name}
            with LOCK:CACHE[name]['value']=value
            try:value={**value,'speed':probe(client,source,name)}
            except Exception:value={**value,'speed':{'status':'unavailable','note':'JSwarm transfer test unavailable; resource observations retained.'}}
    except Exception:
        value={**previous,'host':name,'status':'unavailable','error':'Cannot reach this machine’s JSwarm; retained measurements may be stale.'}
    finally:
        client.close()
        with LOCK:CACHE[name].update(value=value,busy=False,updated=time.monotonic())


def snapshot(name):
    config=json.loads(CONFIG.read_text())
    if name not in config['peers']:raise ValueError('Unknown enrolled machine')
    host={'host':name,'agent_route':'auto','agent_endpoint':'http://'+config['peers'][name]+':8790'}
    with LOCK:
        row=CACHE.setdefault(name,{'value':{'host':name,'status':'loading'},'updated':0,'busy':False})
        if not row['busy'] and time.monotonic()-row['updated']>=1:
            row['busy']=True
            POOL.submit(collect,host,config['name'],row['value'])
        value={**{key:value for key,value in row['value'].items() if not key.startswith('_')},'refreshing':row['busy']}
    from machine_controls import snapshot as control_snapshot
    return {**value,'control':control_snapshot(name)}
