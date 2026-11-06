"""Authenticated LAN jobs and controller-signed owner command pipes."""
import base64
import collections
import contextlib
import fcntl
import hashlib
import json
import math
import io
import os
from pathlib import Path
import re
import secrets
import selectors
import signal
import shutil
import subprocess
import sys
import tarfile
import threading
import time
import urllib.request
import urllib.error


class NvidiaSampler:
    """One node-owned utility process; readers never wait for Docker startup."""
    def __init__(self,changed=lambda:None):
        self.lock=threading.Lock();self.stopped=threading.Event();self.thread=None
        self.rows=[];self.changed=changed;self.retry_at=0;self.last_error=None

    def command(self,name):
        code="""import json,subprocess,time
while True:
 try:
  output=subprocess.check_output(['nvidia-smi','--query-gpu=uuid,pci.bus_id,name,memory.total,memory.free,utilization.gpu,temperature.gpu','--format=csv,noheader,nounits'],text=True,timeout=5)
  print(json.dumps({'output':output,'sampled_at':time.time()}),flush=True)
 except (OSError,subprocess.SubprocessError):print('{}',flush=True)
 time.sleep(2)
"""
        return ['docker','run','--rm','--pull=never','--name',name,'--network','none','--read-only','--cap-drop','ALL',
                '--security-opt','no-new-privileges','--memory','96m','--memory-swap','96m','--cpus','0.25','--pids-limit','128',
                '--label','org.lifetech.service=gpu-inventory','--gpus','all','--env','NVIDIA_DRIVER_CAPABILITIES=utility',
                '--label','org.lifetech.gpu-inventory-owner='+os.environ.get('HOSTNAME',''),
                'organization-launcher:webrtc-1','python3','-u','-c',code]

    def reap_orphans(self):
        result=subprocess.run(['docker','ps','-aq','--filter','label=org.lifetech.gpu-inventory-owner'],capture_output=True,text=True,timeout=15,check=True)
        for ident in result.stdout.split():
            info=json.loads(subprocess.check_output(['docker','inspect',ident],text=True,timeout=15))[0]
            owner=info['Config']['Labels'].get('org.lifetech.gpu-inventory-owner','')
            if not re.fullmatch(r'[a-f0-9]{12,64}',owner):continue
            inspected=subprocess.run(['docker','inspect',owner],capture_output=True,text=True,timeout=15)
            if inspected.returncode:
                if 'No such' not in inspected.stderr:raise RuntimeError('Sampler owner inspection unavailable')
            elif json.loads(inspected.stdout)[0]['State']['Running']:continue
            self.remove(ident)

    def snapshot(self):
        with self.lock:
            if not self.stopped.is_set() and (self.thread is None or not self.thread.is_alive()) and time.monotonic()>=self.retry_at:
                self.thread=threading.Thread(target=self.run,daemon=True,name='nvidia-allocation-sampler');self.thread.start()
            now=time.time()
            return [dict(row) for row in self.rows if -5<=now-row['sampled_at']<=10]

    def accept(self,line):
        value=json.loads(line)
        if not isinstance(value,dict):raise ValueError('Invalid NVIDIA sample')
        sample=value.get('sampled_at')
        rows=[]
        if type(sample) in (float,int) and -5<=time.time()-sample<=10:
            rows=[{**row,'sampled_at':sample} for row in nvidia_allocation_rows(value.get('output',''))]
        with self.lock:
            previous={r['uuid']:r.get('memory_free_mib') for r in self.rows};self.rows=rows
            if rows:self.last_error=None
        # Wake admission when inventory first becomes usable or disappears.
        if previous!={r['uuid']:r.get('memory_free_mib') for r in rows}:self.changed()

    def remove(self,name):
        result=subprocess.run(['docker','rm','-f',name],capture_output=True,text=True,timeout=15)
        if result.returncode and 'No such container' not in result.stderr:raise RuntimeError('NVIDIA sampler cleanup unconfirmed')

    def run(self):
        name='swarm-gpu-inventory-nvidia-'+secrets.token_hex(8);process=None
        try:
            self.reap_orphans()
            process=subprocess.Popen(self.command(name),stdout=subprocess.PIPE,stderr=subprocess.DEVNULL)
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout,selectors.EVENT_READ)
                pending=b'';last=time.monotonic();deadline=60
                while not self.stopped.is_set():
                    if not selector.select(.5):
                        if time.monotonic()-last>deadline:raise TimeoutError('NVIDIA sampler heartbeat unavailable')
                        continue
                    block=os.read(process.stdout.fileno(),65536)
                    if not block:break
                    pending+=block
                    if len(pending)>65536:raise ValueError('Oversized NVIDIA inventory')
                    while b'\n' in pending:
                        line,pending=pending.split(b'\n',1);self.accept(line);last=time.monotonic();deadline=12
        except (OSError,ValueError,RuntimeError,subprocess.SubprocessError) as error:self.last_error=type(error).__name__
        finally:
            if process is not None and process.poll() is None:
                process.terminate()
                try:process.wait(timeout=3)
                except subprocess.TimeoutExpired:process.kill();process.wait()
            if process is not None:process.stdout.close()
            try:self.remove(name)
            except (OSError,RuntimeError,subprocess.SubprocessError):
                # Do not spawn replacements while ownership is uncertain.
                self.retry_at=float('inf')
            with self.lock:
                self.rows=[]
                if self.retry_at!=float('inf'):self.retry_at=time.monotonic()+2
            self.changed()

    def health(self):
        with self.lock:
            sample=max((row['sampled_at'] for row in self.rows),default=None)
            running=self.thread is not None and self.thread.is_alive()
            status=('closed' if self.stopped.is_set() else 'cleanup_pending' if self.retry_at==float('inf') else
                    'fresh' if sample is not None and -5<=time.time()-sample<=10 else
                    'stale' if sample is not None else 'starting' if running else
                    'backoff' if self.retry_at>time.monotonic() else 'idle')
            return {'status':status,'sampled_at':sample,'last_error':self.last_error}

    def close(self):
        self.stopped.set()
        if self.thread:self.thread.join(timeout=20)

def required_test_repositories(suites, jobs=None, repositories=None):
    """Select only reviewed application source dependencies for an owned suite."""
    application={'org-api':('OrgPortal',),'chat':('OrgPortal',),'web':('OrgPortal',),
                 'visual':('OrgPortal',),'identity':('PIdP',),'identity-python':('PIdP',),
                 'usajobs':('CodeCollective',),'lifetech':('BmoreMedTech','OrgPortal')}
    known=set(application)|{'monitor','map','workspace','p2p','swarm-web'}
    needed={'.'}
    if repositories is not None and 'JSwarm' in repositories:
        needed.update(('JSwarm','GenomeHarbor'))
    for suite in suites:
        suite=suite.split('--',1)[0]
        if suite not in known:raise ValueError('Unknown suite source requirements: '+suite)
        if (jobs or {}).get(suite,{}).get('status')=='reused':continue
        needed.update(application.get(suite,()))
    return needed


def sha256_stream(stream):
    """Keep hashing available in the standalone worker bundle on Python 3.10."""
    digest=hashlib.sha256()
    for chunk in iter(lambda:stream.read(1024*1024),b''):digest.update(chunk)
    return digest.hexdigest()


class JobEventWriter:
    """One durable SQLite connection per worker, without per-line checkpoints."""
    def __init__(self, root):
        import sqlite3
        self.path=Path(root)/'test-events.sqlite3'
        self.db=sqlite3.connect(self.path,timeout=30)
        self.db.execute('PRAGMA journal_mode=WAL');self.db.execute('PRAGMA synchronous=FULL')
        self.db.execute('CREATE TABLE IF NOT EXISTS events(job TEXT NOT NULL, sequence INTEGER NOT NULL, payload TEXT NOT NULL, recorded_at REAL NOT NULL, PRIMARY KEY(job,sequence))')
        self.db.commit();self.path.chmod(0o600)

    def append(self,ident,sequence,event):
        with self.db:
            self.db.execute('INSERT INTO events VALUES(?,?,?,?)',(ident,sequence,json.dumps(event,separators=(',',':')),time.time()))

    def close(self):self.db.close()


def persist_job_event(root, ident, job, event, writer=None, publish=True):
    """Commit before publication; a worker may reuse its durable journal."""
    own=writer is None;writer=writer or JobEventWriter(root)
    try:
        sequence=job['sequence']+1
        writer.append(ident,sequence,event)
        if publish:
            job['sequence']=sequence;job['events'].append((sequence,event))
        return sequence
    finally:
        if own:writer.close()


def persisted_job_events(root, ident, cursor):
    import sqlite3
    path=Path(root)/'test-events.sqlite3'
    if not path.exists():return None
    db=sqlite3.connect(path,timeout=30)
    try:
        rows=db.execute('SELECT sequence,payload FROM events WHERE job=? AND sequence>? ORDER BY sequence LIMIT 200',(ident,cursor)).fetchall()
        maximum=db.execute('SELECT max(sequence) FROM events WHERE job=?',(ident,)).fetchone()[0]
        return None if maximum is None else [{'sequence':n,'event':json.loads(value)} for n,value in rows]
    finally:db.close()


def launcher_lock(path):
    """One process per shared config directory, including host/container launches."""
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    lock=path.open('a+')
    if os.geteuid()==0:os.fchown(lock.fileno(),path.parent.stat().st_uid,path.parent.stat().st_gid)
    try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    except BlockingIOError:
        lock.close();raise RuntimeError('A launcher is already running on this machine') from None
    return lock


_WORKER_MEMORY_CACHE={'time':0,'values':{}}
_WORKER_MEMORY_LOCK=threading.Lock()
_WORKER_SAMPLE_START_LOCK=threading.Lock()
_WORKER_SAMPLE_THREAD=None
_WORKER_CPU_CACHE={}
_WORKER_RAM_CACHE={}
_HOST_CPU_SAMPLE={'time':0,'total':0,'idle':0,'busy':None}
_HOST_CPU_LOCK=threading.Lock()
LAUNCH_RESERVATION_SECONDS=60


def worker_memory_usage(ids,refresh=False):
    """Working-set bytes already charged against host MemAvailable; bounded cache."""
    global _WORKER_SAMPLE_THREAD
    if not refresh:
        # Docker's per-container stats can block for seconds each. Admission
        # must never wait for a fleet-sized serial telemetry sweep. Missing or
        # stale samples remain unknown; memory_budget reserves their full RAM.
        values=_WORKER_MEMORY_CACHE['values']
        if time.monotonic()-_WORKER_MEMORY_CACHE['time']<5 and all(i in values for i in ids):return values
        with _WORKER_SAMPLE_START_LOCK:
            if _WORKER_SAMPLE_THREAD is None or not _WORKER_SAMPLE_THREAD.is_alive():
                _WORKER_SAMPLE_THREAD=threading.Thread(target=worker_memory_usage,args=(list(ids),True),daemon=True,name='worker-resource-sampler')
                _WORKER_SAMPLE_THREAD.start()
        return values
    with _WORKER_MEMORY_LOCK:
        now=time.monotonic()
        for cache in (_WORKER_CPU_CACHE,_WORKER_RAM_CACHE):
            for key in [key for key,value in cache.items() if now-value['time']>60]:cache.pop(key,None)
        if not refresh and time.monotonic()-_WORKER_MEMORY_CACHE['time']<5 and all(i in _WORKER_MEMORY_CACHE['values'] for i in ids):return _WORKER_MEMORY_CACHE['values']
        values={}
        try:
            import docker
            with contextlib.closing(docker.from_env(timeout=4)) as client:
                for ident in ids:
                    try:
                        stats=client.api.stats(ident,stream=False,one_shot=True);memory=stats.get('memory_stats',{})
                        sample_time=time.monotonic();cpu_total=stats.get('cpu_stats',{}).get('cpu_usage',{}).get('total_usage')
                        previous=_WORKER_CPU_CACHE.get(ident)
                        cpu_used=None
                        if cpu_total is not None and previous and sample_time-previous['time']>=.5 and cpu_total>=previous['total']:
                            cpu_used=(cpu_total-previous['total'])/1e9/(sample_time-previous['time'])
                        if cpu_total is not None:
                            sample={'time':sample_time,'sampled_at':time.time(),'total':cpu_total,'used':cpu_used,'first':previous.get('first',sample_time) if previous else sample_time}
                            for key in (ident,stats.get('id'),stats.get('name','').lstrip('/')):
                                if key:_WORKER_CPU_CACHE[key]=sample
                        inactive=memory.get('stats',{}).get('total_inactive_file',memory.get('stats',{}).get('inactive_file',0))
                        amount=max(0,memory.get('usage',0)-inactive)//1024**2
                        previous_ram=_WORKER_RAM_CACHE.get(ident,{})
                        ram_sample={'sampled_at':time.time(),'raw_bytes':memory.get('usage',0),
                                    'raw_peak_bytes':max(memory.get('usage',0),memory.get('max_usage',0),previous_ram.get('raw_peak_bytes',0)),
                                    'time':sample_time,'used':amount,'peak':max(amount,previous_ram.get('peak',amount)),
                                    'growth':max(0,amount-previous_ram.get('used',amount)),'first':previous_ram.get('first',sample_time)}
                        for name in (ident,stats.get('id'),stats.get('name','').lstrip('/')):
                            if name:
                                values[name]=amount;_WORKER_RAM_CACHE[name]=ram_sample
                    except (docker.errors.DockerException,OSError,ValueError,KeyError):continue
        except ImportError:
            try:
                result=subprocess.run(['docker','stats','--no-stream','--no-trunc','--format','{{json .}}',*ids],capture_output=True,text=True,timeout=8)
                if result.returncode==0:
                    scale={'B':1,'KiB':1024,'MiB':1024**2,'GiB':1024**3,'TiB':1024**4,'kB':1000,'MB':1000**2,'GB':1000**3}
                    for line in result.stdout.splitlines():
                        row=json.loads(line);match=re.fullmatch(r'([0-9.]+)\s*([A-Za-z]+)',row['MemUsage'].split('/')[0].strip())
                        if match:
                            amount=int(float(match[1])*scale[match[2]])//1024**2
                            values[row['ID']]=amount
                            if row.get('Name'):values[row['Name']]=amount
            except (OSError,ValueError,KeyError,TypeError,subprocess.TimeoutExpired):pass
        _WORKER_MEMORY_CACHE.update(time=time.monotonic(),values=values)
        return values


def cpu_observation():
    """Host CPU deltas without sleeping or spawning a sampler."""
    with _HOST_CPU_LOCK:
        now=time.monotonic()
        try:
            if now-_HOST_CPU_SAMPLE['time']>=2:
                values=[int(v) for v in Path('/proc/stat').read_text().splitlines()[0].split()[1:9]]
                total=sum(values);idle=values[3]+values[4]
                previous=_HOST_CPU_SAMPLE
                busy=None
                if previous['time'] and now-previous['time']<=10 and total>previous['total']:
                    busy=max(0,min(1,1-(idle-previous['idle'])/(total-previous['total'])))
                _HOST_CPU_SAMPLE.update(time=now,total=total,idle=idle,busy=busy)
            pressure=float(Path('/proc/pressure/cpu').read_text().splitlines()[0].split('avg10=')[1].split()[0])
            return {'cpu_busy_fraction':_HOST_CPU_SAMPLE['busy'],'cpu_pressure_percent':pressure}
        except (OSError,ValueError,IndexError):return {}


def resource_pressure():
    """Kernel PSI includes stalls caused by workloads outside JSwarm too."""
    result={}
    for resource,kinds in (('memory',('some','full')),('io',('full',))):
        try:
            rows={line.split()[0]:line for line in Path('/proc/pressure/'+resource).read_text().splitlines()}
            for kind in kinds:result[resource+'_pressure_'+kind+'_percent']=float(rows[kind].split('avg10=')[1].split()[0])
        except (OSError,KeyError,ValueError,IndexError):pass
    return result


def cpu_headroom(limits,active):
    """Measured host headroom, less commitments not visible in that sample yet."""
    physical=limits['cpus'];requested=sum(r['cpus'] for r in active)
    busy=limits.get('cpu_busy_fraction');pressure=limits.get('cpu_pressure_percent')
    if limits.get('cpu_admission_policy')=='timeshare':
        measured=physical*busy if busy is not None else None
        return {'available_cpus':physical,'reserved_cpus':requested,'charged_cpus':measured,
                'cpu_admission_mode':'timeshare','cpu_quota_ceiling':None,'cpu_measured_busy':measured}
    if busy is None or pressure is None:
        return {'available_cpus':max(0,physical-requested),'reserved_cpus':requested,'charged_cpus':requested,
                'cpu_admission_mode':'strict','cpu_quota_ceiling':physical}
    now=time.monotonic();pending=0;observed=0;growth=0
    for row in active:
        sample=row.get('cpu_observation')
        if sample and sample.get('used') is not None and 0<=now-sample['time']<=10:
            observed+=sample['used']
            if now-sample.get('first',0)<10:pending+=max(0,row['cpus']-sample['used'])
            else:growth+=min(.1,row['cpus'])
        else:pending+=row['cpus']
    measured=max(physical*busy,observed)
    # CPU PSI is a trailing average and includes quota/affinity contention in
    # individual workloads. It must not strand an otherwise idle whole host.
    # Still stop lending early when host utilization corroborates contention.
    blocked=busy>=.9 or (pressure>=10 and busy>=.7)
    charged=measured+pending+growth
    return {'available_cpus':0 if blocked else max(0,physical-charged),
            'reserved_cpus':requested,'charged_cpus':charged,'cpu_admission_mode':'pressure',
            'cpu_quota_ceiling':None,'cpu_startup_commitment':pending,'cpu_measured_busy':measured}


def memory_budget(limits,active):
    """Available RAM and stalls drive admission; protect startup and growth."""
    reserved=sum(row['memory_mb'] for row in active)
    unused=sum(max(0,row['memory_mb']-min(row['memory_mb'],max(0,row.get('resident_mb',0)))) for row in active)
    some=limits.get('memory_pressure_some_percent');full=limits.get('memory_pressure_full_percent')
    if some is None or full is None:
        return {'available_mb':max(0,min(limits['memory_mb']-reserved,limits.get('available_mb',limits['memory_mb'])-unused)),
                'memory_admission_mode':'strict','memory_growth_mb':unused,'memory_safety_mb':0}
    now=time.monotonic();growth=0
    for row in active:
        sample=row.get('memory_observation');limit=row['memory_mb']
        if not sample or not 0<=now-sample['time']<=10:
            # Unknown working sets must not be credited as free RAM.
            growth+=limit;continue
        used=min(limit,max(0,sample['used']))
        if row.get('cache_keeper') or now-sample.get('first',now)<10:allowance=limit-used
        else:allowance=min(limit-used,max(64,used*.25)+max(0,sample.get('peak',used)-used)+sample.get('growth',0)*2)
        growth+=allowance
    safety=max(256,int(limits['memory_mb']*.05))
    headroom=max(0,int(limits.get('available_mb',0)-safety-growth))
    # Moderate page faults and unrelated I/O stalls do not imply RAM exhaustion.
    # Corroborate those signals with actual available RAM after growth protection.
    low_memory=headroom<max(1024,int(limits['memory_mb']*.15))
    # I/O PSI also counts network filesystem waits. It cannot establish RAM
    # exhaustion; local storage pressure is guarded separately by disk counters.
    stalled=some>=1 or full>=.1
    blocked=full>=10 or some>=50 or (low_memory and stalled)
    available=0 if blocked else headroom
    return {'available_mb':available,'memory_admission_mode':'pressure','memory_growth_mb':growth,'memory_safety_mb':safety}


def memory_headroom(limits,active):
    return memory_budget(limits,active)['available_mb']


def docker_workers():
    """Docker is authoritative for running workers and their resource limits."""
    result=subprocess.run(['docker','ps','-q','--filter','label=org.lifetech.worker=true'],capture_output=True,text=True,timeout=10)
    if result.returncode:raise RuntimeError('Cannot inspect worker capacity: Docker unavailable')
    ids=result.stdout.split()
    if not ids:return []
    result=subprocess.run(['docker','inspect',*ids],capture_output=True,text=True,timeout=15)
    if result.returncode:
        # A --rm worker may finish between listing and inspection. Retry once.
        current=subprocess.run(['docker','ps','-q','--filter','label=org.lifetech.worker=true'],capture_output=True,text=True,timeout=10)
        if current.returncode:raise RuntimeError('Cannot inspect worker capacity')
        ids=current.stdout.split()
        if not ids:return []
        result=subprocess.run(['docker','inspect',*ids],capture_output=True,text=True,timeout=15)
        if result.returncode:raise RuntimeError('Worker state changed during capacity inspection; retry admission')
    usage=worker_memory_usage(ids)
    rows=[]
    for item in json.loads(result.stdout):
        if not (item['State'].get('Running') or item['State'].get('Restarting')):continue
        limits=item['HostConfig'];labels=item['Config'].get('Labels') or {}
        cpu=limits.get('NanoCpus',0)/1e9
        hard_memory=limits.get('Memory',0)
        private_memory=limits.get('MemoryReservation',0)
        memory=min(private_memory,hard_memory) if hard_memory>0 and private_memory>0 else max(hard_memory,private_memory)
        if cpu<=0 or memory<=0:raise RuntimeError('Worker has no explicit CPU/RAM limits')
        name=item['Name'].removeprefix('/')
        gpu=bool(labels.get('org.lifetech.accelerator') or labels.get('org.lifetech.gpu-qc-service')=='true'
                 or any('gpu' in group for request in limits.get('DeviceRequests') or [] for group in request.get('Capabilities',[]))
                 or any(str(device.get('PathInContainer','')).startswith(('/dev/dri/','/dev/nvidia')) for device in limits.get('Devices') or []))
        rows.append({'id':item['Id'],'container':name,'job':labels.get('org.lifetech.job',name.removeprefix('genomics-').removeprefix('swarm-job-')),
                     'run':labels.get('org.lifetech.run'),'sink':labels.get('org.lifetech.sink','gpu' if gpu else 'cpu'),
                     'resident_mb':usage.get(item['Id'],0),'cache_keeper':bool(labels.get('org.lifetech.node-cache')),'group':labels.get('org.lifetech.group','workers'),'cpus':cpu,'memory_mb':(memory+1024**2-1)//1024**2,'memory_limit_mb':(max(hard_memory,private_memory)+1024**2-1)//1024**2,
                     'cpu_observation':_WORKER_CPU_CACHE.get(item['Id'],_WORKER_CPU_CACHE.get(name)),
                     'memory_observation':_WORKER_RAM_CACHE.get(item['Id'],_WORKER_RAM_CACHE.get(name)),
                     'pids':limits.get('PidsLimit') or 512,'active':True,'created':0})
    return rows


def job_sink(row):
    """Legacy GPU workers remain in their own queue across an agent update."""
    sink=row.get('sink')
    if sink is None:
        sink='gpu' if row.get('group') in ('gpu-qc-native','gpu-qc-service','gpu-probe') else 'cpu'
    if sink not in ('cpu','gpu'):raise ValueError('Expected CPU or GPU job sink')
    return sink


class ResourceLease:
    """Cross-process CPU/RAM admission; locks release on process death.

    CPU and GPU sinks have independent queues over one physical CPU/RAM pool.
    Within each sink, waiting groups with the smallest
    dominant resource share get the next fitting slot. After 30 seconds CPU
    waiters reserve headroom; GPU waiters age only among currently fitting jobs.
    """
    def __init__(self, group, cpus, memory_mb, container='', directory=None, limits=None, pids=512, sink=None, cpu_burst=False, priority=0):
        if not isinstance(group,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,80}',group):raise ValueError('Invalid resource group')
        if (type(cpus) not in (int,float) or not math.isfinite(cpus) or cpus<=0 or
                type(memory_mb) is not int or memory_mb<1 or type(pids) is not int or pids<1):
            raise ValueError('Invalid resources')
        if type(priority) is not int or not -1000<=priority<=1000:raise ValueError('Invalid resource priority')
        if type(cpu_burst) is not bool:raise ValueError('Invalid CPU burst policy')
        if container and not re.fullmatch(r'[a-zA-Z0-9_.-]{1,160}',container):raise ValueError('Invalid container')
        self.directory=Path(directory or os.getenv('SWARM_RESOURCE_DIR',str(Path.home()/'.local/state/swarm-control/resources')))
        self.directory.mkdir(parents=True,exist_ok=True,mode=0o700)
        self.limits=limits
        self.key=secrets.token_hex(16)
        self.path=self.directory/(self.key+'.json')
        self.row={'id':self.key,'group':group,'cpus':cpus,'memory_mb':memory_mb,'pids':pids,'container':container,'created':time.time(),'active':False,'priority':priority}
        self.row['sink']=job_sink({**self.row,**({'sink':sink} if sink is not None else {})})
        if cpu_burst:
            if cpus>0.25 or self.row['sink']!='gpu':raise ValueError('CPU bursting is limited to quarter-CPU GPU feeders')
            self.row['cpu_burst']=True
        with self.guard():
            self.file=self.path.open('x+');self.path.chmod(0o600)
            fcntl.flock(self.file,fcntl.LOCK_EX);self.write()

    def guard(self):
        @contextlib.contextmanager
        def locked():
            with (self.directory/'pool.lock').open('a') as lock:
                fcntl.flock(lock,fcntl.LOCK_EX)
                yield
        return locked()

    def write(self):
        self.file.seek(0);self.file.truncate();json.dump(self.row,self.file);self.file.flush()

    def capacity(self):
        if self.limits is not None:return self.limits
        memory={line.split(':',1)[0]:int(line.split()[1])//1024 for line in Path('/proc/meminfo').read_text().splitlines() if len(line.split())>=2 and line.split()[1].isdigit()}
        limits={'cpus':max(1,len(os.sched_getaffinity(0))), 'memory_mb':max(0,memory['MemTotal']),
                'available_mb':max(0,memory['MemAvailable'])}
        # Respect every finite cgroup ancestor, including the user/service slice
        # shared by rootless Docker. Reserve room for Docker and agent threads.
        root=Path('/sys/fs/cgroup')
        group=next((line.split(':',2)[2] for line in Path('/proc/self/cgroup').read_text().splitlines() if line.startswith('0::')), '/')
        current=root/group.lstrip('/')
        bounds=[]
        memory_bounds=[]
        ancestors=[current,*current.parents]
        container_scope=next((parent for parent in ancestors if re.fullmatch(r'(?:docker-|libpod-)?[a-f0-9]{64}(?:\.scope)?',parent.name)),None)
        # Docker batches are sibling containers, not descendants of this agent.
        # A private cgroup namespace may hide shared ancestors completely.
        private_namespace=container_scope is None and Path('/.dockerenv').exists()
        limits['memory_scope']='host available RAM and visible shared worker cgroup ancestors'
        if private_namespace:limits['memory_scope']='host available RAM; agent-private cgroup excluded; shared cgroup ancestors not visible'
        for parent in [current,*current.parents]:
            if parent!=root and root not in parent.parents:continue
            shared_memory_scope=not private_namespace and (container_scope is None or parent in container_scope.parents)
            if shared_memory_scope:
                try:
                    maximum=(parent/'memory.max').read_text().strip()
                    if maximum!='max':memory_bounds.append((int(maximum)//1024**2,int((parent/'memory.current').read_text())//1024**2))
                except FileNotFoundError:pass
            try:
                maximum=(parent/'pids.max').read_text().strip()
                if maximum!='max':bounds.append((int(maximum),int((parent/'pids.current').read_text())))
            except FileNotFoundError:continue
        if memory_bounds:
            limits['memory_mb']=min(limits['memory_mb'],*(maximum for maximum,_ in memory_bounds))
            limits['available_mb']=max(0,min(limits['available_mb'],*(maximum-used for maximum,used in memory_bounds)))
        if bounds:
            limits['pids']=max(0,min(maximum-min(256,max(32,maximum//4)) for maximum,_ in bounds))
            limits['available_pids']=max(0,min(maximum-used-min(256,max(32,maximum//4)) for maximum,used in bounds))
        limits.update(cpu_observation());limits.update(resource_pressure())
        policy=os.getenv('SWARM_CPU_ADMISSION','timeshare')
        if policy not in ('timeshare','pressure'):raise ValueError('Invalid CPU admission policy')
        limits['cpu_admission_policy']=policy
        return limits

    @classmethod
    def pool_capacity(cls, directory=None, group=None, *, locked=False):
        """Read admission headroom under the pool lock, without making a reservation."""
        probe=cls.__new__(cls)
        probe.limits=None
        probe.directory=Path(directory or os.getenv('SWARM_RESOURCE_DIR',str(Path.home()/'.local/state/swarm-control/resources')))
        probe.directory.mkdir(parents=True,exist_ok=True,mode=0o700)
        probe.path=probe.directory/'.capacity'
        with contextlib.nullcontext() if locked else probe.guard():
            limits=probe.capacity();rows=probe.rows();active=[row for row in rows if row['active']]
            reserved=sum(row['memory_mb'] for row in active)
            # Host pressure and observed growth drive capacity; unknown starts
            # retain their complete commitments until measurements arrive.
            sinks={kind:{'active_jobs':sum(r['active'] and job_sink(r)==kind for r in rows),
                         'waiting_jobs':sum(not r['active'] and job_sink(r)==kind for r in rows),
                         'group_active_jobs':sum(r['group']==group and job_sink(r)==kind for r in active),
                         'reserved_cpus':sum(r['cpus'] for r in active if job_sink(r)==kind),
                         'reserved_mb':sum(r['memory_mb'] for r in active if job_sink(r)==kind),
                         'measured_host_memory_mb':sum(max(0,r['memory_observation']['used']) for r in active if job_sink(r)==kind and r.get('memory_observation') and 0<=time.monotonic()-r['memory_observation']['time']<=10),
                         'host_memory_measured_jobs':sum(bool(r.get('memory_observation')) and 0<=time.monotonic()-r['memory_observation']['time']<=10 for r in active if job_sink(r)==kind),
                         'memory_domain':'host-ram',
                         'measurement_basis':'Fresh container memory observations; GPU VRAM is reported separately by resource.gpus'}
                   for kind in ('cpu','gpu')}
            return {**limits,**memory_budget(limits,active),'reserved_mb':reserved,'job_sinks':sinks,
                    **cpu_headroom(limits,active),
                    'active_jobs':len(active),'group_active_jobs':sum(row['group']==group for row in active),
                    'waiting_jobs':sum(not row['active'] for row in rows)}

    def rows(self):
        rows=[]
        for path in self.directory.glob('*.json'):
            if path==self.path:rows.append(dict(self.row));continue
            try:
                with path.open('r+') as file:
                    row=json.load(file)
                    try:fcntl.flock(file,fcntl.LOCK_EX|fcntl.LOCK_NB)
                    except BlockingIOError:rows.append(row);continue
                    if row.get('handover_until',0)>time.time():rows.append(row);continue
                    # Keep an orphan's reservation until its container is gone.
                    if row.get('active') and row.get('container'):
                        check=subprocess.run(['docker','inspect','-f','{{.State.Running}}',row['container']],capture_output=True,text=True,timeout=5)
                        if check.returncode==0 and check.stdout.strip()=='true':rows.append(row);continue
                    path.unlink(missing_ok=True)
            except FileNotFoundError:continue
        if (self.directory/'docker-workers').exists():
            tracked={r.get('container') for r in rows}
            workers=docker_workers();observed={row['container']:row for row in workers}
            for row in rows:
                if row.get('container') in observed:
                    row['resident_mb']=observed[row['container']].get('resident_mb',0)
                    row['cpu_observation']=observed[row['container']].get('cpu_observation')
                    row['memory_observation']=observed[row['container']].get('memory_observation')
                    row['sink']=job_sink(observed[row['container']])
            rows.extend(row for row in workers if row['container'] not in tracked)
        return rows

    def try_acquire(self):
        with self.guard():
            if self.row['active']:return True
            try:limits=self.capacity();rows=self.rows()
            except subprocess.TimeoutExpired:
                # Unknown Docker state cannot admit work, but is not a failed job.
                # Keep the queued reservation and retry when the daemon responds.
                return False
            active=[r for r in rows if r['active']]
            if (self.directory/'docker-workers').exists():
                names=[r['container'] for r in active if r.get('container')]
                usage=worker_memory_usage(names) if names else {}
                for row in active:
                    row['resident_mb']=usage.get(row.get('container'),0)
                    row['cpu_observation']=_WORKER_CPU_CACHE.get(row.get('container'),row.get('cpu_observation'))
                    row['memory_observation']=_WORKER_RAM_CACHE.get(row.get('container'),row.get('memory_observation'))
            keys=('cpus','memory_mb')+(('pids',) if 'pids' in limits else ())
            free={k:limits[k]-sum(r.get(k,512) for r in active) for k in keys}
            cpu_budget=cpu_headroom(limits,active)
            free['cpus']=cpu_budget['available_cpus'] if cpu_budget['cpu_admission_mode']=='timeshare' else min(cpu_budget['available_cpus'],limits['cpus']-cpu_budget['charged_cpus'])
            free['memory_mb']=memory_headroom(limits,active)
            if 'pids' in free:free['pids']=min(free['pids'],limits.get('available_pids',limits['pids']))
            if any(self.row[k]>limits[k] for k in keys):raise ValueError('Task exceeds node resource capacity')
            # A blocked GPU must not hold the CPU queue's future headroom,
            # and a large CPU waiter must not stop a fitting GPU feeder.
            waiting=sorted((r for r in rows if not r['active']),key=lambda r:(r['created'],r['id']))
            def fits(row):
                # Explicit GPU feeders may time-share at most one extra quarter
                # CPU across the physical pool. RAM and PID bounds stay strict.
                extra=0.25 if row.get('cpu_burst') is True and cpu_budget['cpu_admission_mode']=='strict' else 0
                return all(row[k]<=free[k]+(extra if k=='cpus' else 0) for k in free)
            def share(row):
                used={k:sum(r.get(k,512) for r in active if r['group']==row['group'] and job_sink(r)==job_sink(row)) for k in free}
                return max(used[k]/max(1,limits[k]) for k in free),row['created'],row['id']
            candidates=[]
            now=time.time()
            for sink in ('cpu','gpu'):
                queue=[r for r in waiting if job_sink(r)==sink]
                if not queue:continue
                # A GPU request that cannot fit must not reserve the entire
                # GPU queue's future headroom or hide fitting lower-priority
                # work. Preserve priority and age among admissible requests.
                if sink=='gpu':queue=[r for r in queue if fits(r)]
                if not queue:continue
                highest=max(r.get('priority',0) for r in queue)
                queue=[r for r in queue if r.get('priority',0)==highest]
                oldest=queue[0]
                if now-oldest['created']>=30:
                    candidate=oldest if fits(oldest) else None
                else:candidate=min((r for r in queue if fits(r)),key=share,default=None)
                if candidate:candidates.append(candidate)
            # Once an aged request fits, the other sink cannot repeatedly steal
            # that shared RAM before its controller's next admission poll.
            def priority(row):
                return (-row.get('priority',0),0,row['created'],row['id']) if now-row['created']>=30 else (-row.get('priority',0),1,*share(row))
            chosen=min(candidates,key=priority,default=None)
            if not chosen or chosen['id']!=self.key:return False
            self.row['active']=True;self.write();return True

    def close(self):
        if self.file.closed:return
        with self.guard():self.path.unlink(missing_ok=True);self.file.close()


VERSION = 'lifetech-agent.v1'
LIMIT = 1024 * 1024
UNPRIVILEGED_OPS={'status','metrics','metrics.publish','metrics.fleet','worker.policy','image.inspect','registry.read','tests.request','tests.status','stream.capabilities'}


def gpu_worker_commitments(released=()):
    """Docker ownership metadata only; avoid another per-worker stats scan."""
    try:
        result=subprocess.run(['docker','ps','--filter','label=org.lifetech.accelerator','--format','{{json .Labels}}'],capture_output=True,text=True,check=True,timeout=5)
        devices={}
        for line in result.stdout.splitlines():
            value=json.loads(line)
            labels=value if isinstance(value,dict) else dict(part.split('=',1) for part in value.split(',') if '=' in part)
            if labels.get('org.lifetech.stream-work') in released:continue
            uuid=labels.get('org.lifetech.accelerator')
            if not uuid:continue
            row=devices.setdefault(uuid,{'active_workers':0,'private_commitment_mib':0,'exclusive_worker_present':False})
            concurrency=int(labels.get('org.lifetech.gpu-concurrency','1'))
            row['active_workers']+=1;row['private_commitment_mib']+=int(labels.get('org.lifetech.gpu-private-mib','0'))
            row['exclusive_worker_present']|=concurrency<=1 and labels.get('org.lifetech.gpu-admission')!='pressure'
        return {'known':True,'devices':devices}
    except (OSError,ValueError,TypeError,subprocess.SubprocessError):return {'known':False,'devices':{}}


def drm_memory_allocation(device):
    """Keep driver-local VRAM and host-backed GTT as separate pools."""
    def counter(name):
        try:
            value=int((device/name).read_text().strip())
            return value if value>=0 else None
        except (OSError,ValueError):return None
    total=counter('mem_info_vram_total');used=counter('mem_info_vram_used')
    gtt_total=counter('mem_info_gtt_total');gtt_used=counter('mem_info_gtt_used')
    result={'memory_source':'DRM sysfs','memory_total_mib':total/1024**2 if total is not None else None,
            'memory_used_mib':used/1024**2 if used is not None else None,
            'memory_free_mib':max(0,total-used)/1024**2 if total is not None and used is not None else None,
            'memory_domain':'driver-reserved-vram' if total else 'shared-system-memory' if gtt_total else 'unknown',
            'gtt_total_mib':gtt_total/1024**2 if gtt_total is not None else None,
            'gtt_used_mib':gtt_used/1024**2 if gtt_used is not None else None,
            'gtt_accounting':'host-backed aperture; not additional GPU VRAM',
            'gpu_allocation_added_to_host_ram':False}
    busy=counter('gpu_busy_percent')
    if busy is not None:result['utilization_percent']=busy
    return result


def nvidia_allocation_rows(output):
    import csv
    rows=[]
    for fields in csv.reader(output.splitlines()):
        values=[v.strip() for v in fields]
        uuid=values.pop(0) if len(values)==7 else None
        ident,name,total,free,util,temp=values
        def number(value):
            try:return float(value)
            except ValueError:return None
        pci=re.fullmatch(r'([0-9a-fA-F]{4,8}):([0-9a-fA-F]{2}:[0-9a-fA-F]{2}\.[0-7])',ident)
        if pci:ident=f'{int(pci[1],16):04x}:'+pci[2].lower()
        rows.append({'uuid':uuid,'pci_id':ident,'vendor':'NVIDIA','name':name,'backend':'CUDA candidate',
                     'memory_domain':'driver-reserved-vram','memory_source':'NVIDIA driver','gpu_allocation_added_to_host_ram':False,
                     'memory_total_mib':number(total),'memory_free_mib':number(free),
                     'utilization_percent':number(util),'temperature_c':number(temp)})
    return rows


def hardware_sample(root=Path('/sys/bus/pci/devices'), disk=Path.home(), query_nvidia=True):
    """Read hardware without treating GPU detection as container validation."""
    usage=shutil.disk_usage(disk)
    result={'storage':{'path':str(disk),'total_bytes':usage.total,'free_bytes':usage.free},'gpus':[]}
    vendors={'0x10de':'NVIDIA','0x8086':'Intel','0x1002':'AMD'}
    for path in sorted(root.glob('*')):
        try:
            if not (path/'class').read_text().strip().startswith('0x03'):continue
            vendor=vendors.get((path/'vendor').read_text().strip(),'Other')
            row={'pci_id':path.name,'vendor':vendor,
                'device_id':(path/'device').read_text().strip(),'vendor_id':(path/'vendor').read_text().strip(),
                'name':vendor+' GPU '+(path/'device').read_text().strip(),
                'backend':'CUDA candidate' if vendor=='NVIDIA' else 'Graphics / backend unverified'}
            renders=sorted((path/'drm').glob('renderD*'))
            if renders:
                row['render_device']='/dev/dri/'+renders[0].name
                try:row['render_gid']=Path(row['render_device']).stat().st_gid
                except OSError:row['render_gid']=None
            if vendor in ('AMD','Intel'):row.update(drm_memory_allocation(path))
            result['gpus'].append(row)
        except OSError:continue
    if query_nvidia and shutil.which('nvidia-smi'):
        import csv
        try:
            output=subprocess.check_output(['nvidia-smi','--query-gpu=uuid,pci.bus_id,name,memory.total,memory.free,utilization.gpu,temperature.gpu','--format=csv,noheader,nounits'],text=True,stderr=subprocess.DEVNULL,timeout=2)
            rows=nvidia_allocation_rows(output)
            result['gpus']=[g for g in result['gpus'] if g['vendor']!='NVIDIA']+rows
        except (OSError,ValueError,subprocess.SubprocessError):pass
    return result


GPU_SMOKE = r'''import ctypes as c,json
lib=c.CDLL('libcuda.so.1')
def api(name,types):
 f=getattr(lib,name);f.argtypes=types;f.restype=c.c_int
 def call(*args):
  code=f(*args)
  if code:raise RuntimeError(name+' returned CUDA error '+str(code))
 return call
P=c.c_void_p;I=c.c_int;U=c.c_uint;S=c.c_size_t;D=c.c_uint64
api('cuInit',[U])(0);count=I();api('cuDeviceGetCount',[c.POINTER(I)])(c.byref(count))
if count.value<1:raise RuntimeError('No CUDA devices exposed to the container')
ptx=b'.version 6.0\n.target sm_52\n.address_size 64\n.visible .entry check_value(.param .u64 out){.reg .u64 %rd; .reg .u32 %r; ld.param.u64 %rd,[out]; mov.u32 %r,42; st.global.u32 [%rd],%r; ret;}\n'
rows=[]
for device in range(count.value):
 name=c.create_string_buffer(256);total=S();ctx=P();module=P();func=P();address=D()
 api('cuDeviceGetName',[P,I,I])(name,256,device)
 api('cuDeviceTotalMem_v2',[c.POINTER(S),I])(c.byref(total),device)
 api('cuCtxCreate_v2',[c.POINTER(P),U,I])(c.byref(ctx),0,device)
 try:
  api('cuModuleLoadData',[c.POINTER(P),P])(c.byref(module),c.cast(c.c_char_p(ptx),P))
  api('cuModuleGetFunction',[c.POINTER(P),P,c.c_char_p])(c.byref(func),module,b'check_value')
  api('cuMemAlloc_v2',[c.POINTER(D),S])(c.byref(address),4)
  params=(P*1)(c.cast(c.byref(address),P))
  api('cuLaunchKernel',[P,U,U,U,U,U,U,U,P,P,P])(func,1,1,1,1,1,1,0,None,params,None)
  api('cuCtxSynchronize',[])()
  output=I();api('cuMemcpyDtoH_v2',[P,D,S])(c.byref(output),address,4)
  if output.value!=42:raise RuntimeError('GPU kernel readback mismatch')
  rows.append({'name':name.value.decode(),'memory_total_mib':round(total.value/2**20),'kernel_result':output.value})
 finally:api('cuCtxDestroy_v2',[P])(ctx)
print(json.dumps({'devices':rows,'status':'passed'}))
'''

# Workspaces are disposable; coordinator results and small job receipts are separate.
WORKSPACE_BUDGET=5*1024**3
MIN_FREE_BYTES=10*1024**3

def tree_bytes(root):
    total=0;seen=set()
    for folder,dirs,files in os.walk(root,followlinks=False):
        dirs[:]=[name for name in dirs if not (Path(folder)/name).is_symlink()]
        for name in files:
            path=Path(folder)/name
            try:
                stat=path.lstat();identity=(stat.st_dev,stat.st_ino)
                if not path.is_symlink() and identity not in seen:total+=stat.st_blocks*512;seen.add(identity)
            except FileNotFoundError:pass
    return total

def prune_workspaces(root,active=(),mounts=(),*,now=None,budget=WORKSPACE_BUDGET,min_free=MIN_FREE_BYTES):
    root=Path(root);now=time.time() if now is None else now
    active=set(active);records=[];removed=[]
    for state_path in root.glob('*/state.json'):
        folder=state_path.parent
        if folder.is_symlink() or not re.fullmatch(r'[0-9TZ-]+-[a-f0-9]{8}',folder.name):continue
        try:
            if state_path.is_symlink() or state_path.stat().st_size>1024*1024:continue
            state=json.loads(state_path.read_text())
        except (OSError,ValueError):continue
        if folder.name in active or state.get('status') not in ('completed','failed','stopped','interrupted','consumed'):continue
        if any(str(mount)==str(folder) or str(mount).startswith(str(folder)+'/') for mount in mounts):continue
        source=folder/'source'
        if source.is_symlink() or (folder/'results').is_symlink():continue
        finished=state.setdefault('finished_at',state_path.stat().st_mtime)
        size=state.get('workspace_bytes')
        if size is None:size=tree_bytes(source) if source.exists() else 0;state['workspace_bytes']=size
        records.append((finished,folder,state,size))
    records.sort(key=lambda row:row[0],reverse=True)
    failed=[folder.name for finished,folder,state,size in records if source_exists(folder) and state['status'] in ('failed','interrupted') and now-finished<86400][:2]
    prepared=[folder.name for finished,folder,state,size in records if source_exists(folder) and state.get('prepare_only') and state['status']=='completed' and now-finished<1800][:1]
    keep=set(failed+prepared);retained=sum(size for _,folder,_,size in records if folder.name in keep)
    retained+=sum(tree_bytes(folder/'results') for _,folder,_,_ in records)
    for finished,folder,state,size in reversed(records):
        source=folder/'source'
        pressure=shutil.disk_usage(root).free<min_free
        if source.exists() and (folder.name not in keep or retained>budget or pressure):
            # Keep bounded diagnostics independently of the installed dependencies.
            results=folder/'results';before=tree_bytes(results);results.mkdir(exist_ok=True,mode=0o700)
            for relative in ('visual-artifacts','swarm-web-artifacts','.local/test-status','OrgPortal/.local/test-status'):
                origin=source/relative
                if origin.is_dir() and not origin.is_symlink() and origin.resolve().is_relative_to(source.resolve()) and tree_bytes(origin)<=64*1024**2:
                    def ignore_links(directory,names):return [name for name in names if (Path(directory)/name).is_symlink()]
                    shutil.copytree(origin,results/relative.replace('/','-'),dirs_exist_ok=True,ignore=ignore_links)
            retained+=tree_bytes(results)-before
            shutil.rmtree(source)
            state.update(workspace_retained=False,workspace_bytes=0)
            removed.append(folder.name)
            if folder.name in keep:retained-=size
        temporary=folder/'state.json.tmp';temporary.write_text(json.dumps(state));temporary.replace(folder/'state.json')
        if not source.exists() and now-finished>30*86400:shutil.rmtree(folder)
    for _,folder,state,_ in reversed(records):
        results=folder/'results'
        if retained>budget and results.exists():
            size=tree_bytes(results);shutil.rmtree(results);retained-=size
    return {'removed':removed,'retained_bytes':retained,'workspace_budget_bytes':budget,
            'free_bytes':shutil.disk_usage(root).free,'minimum_free_bytes':min_free,'checked_at':now}

def prune_package_cache(root,mounts=(),budget=5*1024**3):
    root=Path(root)
    if not root.is_dir() or root.is_symlink():return 0
    entries=[]
    for path in root.iterdir():
        if not path.is_dir() or path.is_symlink():continue
        stamp=path/'.last-used';used=stamp.stat().st_mtime if stamp.exists() else path.stat().st_mtime
        entries.append((used,path,tree_bytes(path)))
    total=sum(size for _,_,size in entries)
    for used,path,size in sorted(entries):
        if total<=budget:break
        if any(str(mount)==str(path) or str(mount).startswith(str(path)+'/') for mount in mounts):continue
        with (root/(path.name+'.lock')).open('a') as lock:
            try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:continue
            shutil.rmtree(path);total-=size
    return total

def source_exists(folder):return (folder/'source').is_dir()


def verify_controller(value, config):
    if value.get('op') in UNPRIVILEGED_OPS:return
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    if value.get('from')!=config.get('master','compai.local'):raise ValueError('Execution requires the enrolled controller')
    signed={k:v for k,v in value.items() if k!='controller_signature'}
    try:
        key=Ed25519PublicKey.from_public_bytes(bytes.fromhex(config['controller_public_key']))
        key.verify(base64.b64decode(value['controller_signature'],validate=True),json.dumps(signed,sort_keys=True,separators=(',',':')).encode())
    except Exception:raise ValueError('Controller signature rejected') from None




def browser_request(container,path,data,method):
    # Execute inside the owned browser namespace: rootless host networking is
    # separate from the physical host's loopback namespace.
    script="""import json,sys,urllib.request,urllib.error
from pathlib import Path
source=Path(sys.argv[1])
try:value=json.loads(source.read_text())
finally:source.unlink()
body=json.dumps(value['data']).encode() if value['data'] is not None else None
request=urllib.request.Request('http://127.0.0.1:4449'+value['path'],data=body,method=value['method'],headers={'Content-Type':'application/json'})
try:
 response=urllib.request.urlopen(request,timeout=150)
except urllib.error.HTTPError as error:
 response=error
with response:print(response.read(4*1024*1024+1).decode())
"""
    # WebDriver scripts can exceed Linux's per-argument size limit. Keep the
    # bounded request in a private, short-lived container file instead of argv.
    name='webdriver-'+secrets.token_hex(16)+'.json'
    payload=json.dumps({'path':path,'data':data,'method':method}).encode()
    archive=io.BytesIO()
    with tarfile.open(fileobj=archive,mode='w') as stream:
        info=tarfile.TarInfo(name);info.size=len(payload);info.mode=0o600;info.uid=1200;info.gid=1201
        stream.addfile(info,io.BytesIO(payload))
    if not container.put_archive('/tmp',archive.getvalue()):raise RuntimeError('Owned browser request staging failed')
    code,output=container.exec_run(['/usr/bin/python3','-c',script,'/tmp/'+name],user='1200:1201')
    if code or len(output)>4*1024*1024:raise RuntimeError('Owned browser WebDriver request failed')
    return json.loads(output)

def worker_policy(config, power_root=Path('/sys/class/power_supply'), drain_path=Path('/tmp/launcher-agent-draining')):
    """Local owner policy; unknown laptop power fails closed in automatic mode."""
    if drain_path.exists():
        return {'mode':'draining','accepting_jobs':False,'reason':'Swarm-node update: declining new work during process replacement'}
    maintenance=Path(config).parent/'maintenance.json'
    if maintenance.exists():
        return {'mode':'maintenance','accepting_jobs':False,'reason':'Owner maintenance: draining existing work; renewals remain available'}
    path=Path(config).parent/'worker-policy.json'
    try:
        mode=json.loads(path.read_text()).get('mode','auto') if path.exists() else 'auto'
        if mode not in ('auto','always','paused'):raise ValueError('Invalid worker mode')
    except (OSError,ValueError,TypeError):
        return {'mode':'invalid','accepting_jobs':False,'reason':'Worker policy is unreadable'}
    batteries=[];online=[]
    try:
        for device in power_root.iterdir():
            kind=(device/'type').read_text().strip()
            if kind=='Battery':
                if (device/'scope').exists() and (device/'scope').read_text().strip()=='Device':continue
                batteries.append({'status':(device/'status').read_text().strip(),
                                  'percent':int((device/'capacity').read_text())})
            elif (device/'online').exists():online.append((device/'online').read_text().strip()=='1')
    except (OSError,ValueError):
        return {'mode':mode,'accepting_jobs':mode=='always','reason':'Power state unavailable','batteries':batteries}
    on_battery=bool(batteries) and (not any(online) if online else any(b['status']!='Charging' for b in batteries))
    accepting=mode=='always' or (mode=='auto' and not on_battery)
    return {'mode':mode,'accepting_jobs':accepting,'on_battery':on_battery,'batteries':batteries,
            'reason':'Submit only; new worker jobs paused' if mode=='paused' else
                     'Saving battery; new worker jobs paused' if not accepting else 'Accepting worker jobs'}


def local_peer_qc(config, include_inactive=False):
    """Expose only the native user's signed, fixed peer-QC service to its UI."""
    import shlex
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    from cryptography.exceptions import InvalidSignature
    home = Path.home()
    if Path(config).resolve() != (home/'.config/lifetech-launcher/peers.json').resolve():
        return None
    unit = home/'.config/systemd/user/venter-peer-qc.service'
    if not unit.exists():
        return None
    try:
        argv = shlex.split(next(line.split('=', 1)[1] for line in unit.read_text().splitlines() if line.startswith('ExecStart=')))
        if len(argv) != 4 or argv[2] != '--root':
            return None
        root = Path(argv[3]).resolve()
        if root.parent != (home/'.local/state/venter-peer-qc').resolve() or argv[1] != str(root/'worker.py'):
            return None
        envelope = json.loads((root/'manifest.json').read_text()); value = envelope['value']
        enrolled = json.loads(Path(config).read_text())
        key = Ed25519PublicKey.from_public_bytes(bytes.fromhex(enrolled['controller_public_key']))
        key.verify(base64.b64decode(envelope['signature'], validate=True), json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode())
        if value['id'] != root.name or value['expires'] < time.time() or enrolled['name'] not in value['nodes'] or hashlib.sha256((root/'worker.py').read_bytes()).hexdigest() != value['worker_sha256']:
            return None
        running = subprocess.run(['systemctl', '--user', 'is-active', 'venter-peer-qc.service'], capture_output=True, text=True, timeout=5).stdout.strip() == 'active'
        if not running and not include_inactive:
            return None
        state = json.loads((root/'state.json').read_text()) if (root/'state.json').exists() else {}
        count = len(state.get('completed', [])); total = len(value['jobs'])
        return {'id': value['id'], 'kind': 'peer-qc', 'status': 'running' if running else 'stopped',
                'label': f'Venter QC peer · {count}/{total} verified results'}
    except (OSError, ValueError, KeyError, StopIteration, subprocess.SubprocessError, InvalidSignature):
        return None


def start_local_peer_qc(config):
    job = local_peer_qc(config, include_inactive=True)
    if job:
        subprocess.run(['systemctl', '--user', 'start', 'venter-peer-qc.service'], check=True, timeout=15)
    return bool(job)


def worker_environment(home,work=None):
    # The Docker CLI must use the same local engine as this agent, including
    # rootless sockets. Deployment and login credentials stay out of jobs.
    env = {key: os.environ[key] for key in ('PATH', 'LANG', 'LC_ALL', 'DOCKER_HOST') if key in os.environ}
    env.update(HOME=str(home), PYTHONDONTWRITEBYTECODE='1', PYTHONUNBUFFERED='1',
               GIT_TERMINAL_PROMPT='0', GIT_CONFIG_NOSYSTEM='1')
    if work is not None:
        work=Path(work)
        env['PYTHONPATH']=os.pathsep.join(str(p) for p in (work,work/'scripts',work/'swarm-control/server'))
    return env


METRIC_SAMPLE = "import json,time,socket,subprocess\nfrom pathlib import Path\nresult={'monotonic':time.monotonic(),'sampled_at':time.time(),'cpu_counters':None,'cpu_error':None,\n        'ram_percent':None,'ram_used_bytes':None,'ram_total_bytes':None,'disk_counters':{},'network_bytes':None,\n        'disk_basis':'Busiest physical block device; Linux I/O busy time','cpu_pressure_some_percent':None}\ntry:\n result['boot_id']=Path('/proc/sys/kernel/random/boot_id').read_text().strip()\nexcept OSError:result['boot_id']=None\ntry:\n result['cpu_counters']={v[0]:[int(x) for x in v[1:9]] for line in Path('/proc/stat').read_text().splitlines() if (v:=line.split()) and (v[0]=='cpu' or v[0].startswith('cpu') and v[0][3:].isdigit())}\nexcept PermissionError:result['cpu_error']='permission_denied'\nexcept (OSError,ValueError):result['cpu_error']='unavailable'\ntry:\n memory={k:int(v.split()[0])*1024 for line in Path('/proc/meminfo').read_text().splitlines() for k,v in [line.split(':',1)]}\n total=memory['MemTotal'];used=total-memory['MemAvailable']\n result.update(ram_total_bytes=total,ram_used_bytes=used,ram_percent=round(100*used/total,1))\nexcept (OSError,ValueError,KeyError):pass\ntry:\n for device in Path('/sys/block').iterdir():\n  if '/virtual/' in str(device.resolve()):continue\n  try:result['disk_counters'][device.name]=int((device/'stat').read_text().split()[9])\n  except (OSError,ValueError,IndexError):pass\nexcept OSError:pass\ntry:\n counters=[]\n for device in Path('/sys/class/net').iterdir():\n  if device.name=='lo' or '/virtual/' in str(device.resolve()):continue\n  counters.append(sum(int((device/'statistics'/field).read_text()) for field in ('rx_bytes','tx_bytes')))\n result['network_bytes']=sum(counters) if counters else None\nexcept (OSError,ValueError):pass\ntry:\n pressure=next(line.split() for line in Path('/proc/pressure/cpu').read_text().splitlines() if line.startswith('some '))\n result['cpu_pressure_some_percent']=float(dict(v.split('=') for v in pressure[1:])['avg10'])\nexcept (OSError,ValueError,KeyError,StopIteration):pass\ntry:result['addresses']=subprocess.check_output(['hostname','-I'],text=True,timeout=1).split()\nexcept (OSError,subprocess.SubprocessError):result['addresses']=[]\nresult['observed_hostname']=socket.gethostname()\nprint(json.dumps(result))\n"

class ResourceSampler:
    """Counter deltas span the entire interval between snapshots, including RPC gaps."""
    def __init__(self, reader=None):
        self.reader=reader or self.capture
        self.previous=None
        self.lock=threading.Lock()

    @staticmethod
    def capture():
        result=subprocess.run([sys.executable,'-c',METRIC_SAMPLE],capture_output=True,text=True,timeout=5,check=True)
        return json.loads(result.stdout)

    def sample(self):
        with self.lock:
            current=self.reader();previous=self.previous;self.previous=current
            counters=current['cpu_counters']
            result={k:v for k,v in current.items() if k not in ('cpu_counters','disk_counters','network_bytes','monotonic','boot_id')}
            result.update(measurement_version=2,percent=None,logical_cpus=[],window_seconds=None,
                          cpu_iowait_percent=None,cpu_steal_percent=None,network_mbps=None,disk_busy_percent=None,
                          cpu_busy_logical_cpus=None)
            result['cpus']=len([k for k in counters or {} if k!='cpu']) or None
            if current.get('cpu_error'):
                result['status']=current['cpu_error'];return result
            elapsed=current['monotonic']-previous['monotonic'] if previous else 0
            if not previous or previous.get('boot_id')!=current.get('boot_id') or not 0<elapsed<=30:
                result['status']='warming';return result
            def delta(key):
                before=(previous.get('cpu_counters') or {}).get(key);after=(counters or {}).get(key)
                if before is None or after is None or len(before)!=len(after):return None
                values=[b-a for a,b in zip(before,after)]
                return values if all(v>=0 for v in values) and sum(values)>0 else None
            def percent(values,index=None):
                if values is None:return None
                value=values[index] if index is not None else sum(values)-values[3]-values[4]-values[7]
                return round(100*value/sum(values),1)
            aggregate=delta('cpu')
            if aggregate is None or set(previous.get('cpu_counters') or {})!=set(counters or {}):
                result['status']='warming';return result
            result.update(status='live',percent=percent(aggregate),window_seconds=round(elapsed,3),
                          cpu_iowait_percent=percent(aggregate,4),cpu_steal_percent=percent(aggregate,7),
                          logical_cpus=[{'id':int(k[3:]),'percent':percent(delta(k))} for k in counters if k!='cpu'])
            result['cpu_busy_logical_cpus']=round(result['percent']*result['cpus']/100,2)
            disks=[100*(value-previous.get('disk_counters',{}).get(k,value))/(elapsed*1000)
                   for k,value in current.get('disk_counters',{}).items()
                   if k in previous.get('disk_counters',{}) and value>=previous['disk_counters'][k]]
            result['disk_busy_percent']=round(min(100,max(disks)),1) if disks else None
            if current.get('network_bytes') is not None and previous.get('network_bytes') is not None:
                change=current['network_bytes']-previous['network_bytes']
                if change>=0:result['network_mbps']=round(change*8/elapsed/1000000,4)
            return result


# Local telemetry collectors live in the atomic launcher release. They never SSH.
import copy
import tempfile
PROFILE_LOCK=threading.Lock()


def telemetry_capture(script):
    result=subprocess.run([sys.executable,'-c',script],capture_output=True,text=True,timeout=4,check=True)
    if len(result.stdout)>65536:raise ValueError('Telemetry response exceeds limit')
    return json.loads(result.stdout)


INTERFACE_SCRIPT = r'''import json,time,re,subprocess
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


class InterfaceSampler:
    def __init__(self,reader=None):
        self.reader=reader or (lambda target: telemetry_capture(INTERFACE_SCRIPT));self.previous=None;self.seen={}

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


DISK_SCRIPT=r'''import json,time,hashlib
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
    def __init__(self,reader=None,capacities=None):
        self.reader=reader or (lambda target:telemetry_capture(DISK_SCRIPT));self.previous=None;self.capacities=Path(capacities or Path.home()/'.config/lifetech-launcher/disk-capacities.json');self.windows={}

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
                    frames=self.windows.setdefault(after['identity'],collections.deque())
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


GPU_SCRIPT=r'''import json,time,re
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
# Docker's host PID namespace does not grant access to host fd links.
# Native telemetry supplies host GPU clients without triggering AppArmor denials.
for proc in ([] if Path('/.dockerenv').exists() else Path('/proc').glob('[0-9]*')):
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
        self.reader=reader or (lambda target:telemetry_capture(GPU_SCRIPT));self.previous=None

    def sample(self,target,hardware):
        try:
            current=self.reader(target);rows=activity(self.previous,current);self.previous=current
        except (OSError,ValueError,KeyError,TypeError,subprocess.SubprocessError):return hardware
        return {**hardware,'gpus':[{**gpu,**rows.get(gpu.get('pci_id'),{})} for gpu in hardware.get('gpus',[])]}


COMMAND_RUNTIME_SOURCE = ''


def load_command_runtime():
    """The command channel ships inside the existing verified agent release."""
    import types
    source=COMMAND_RUNTIME_SOURCE or (Path(__file__).resolve().parent/'launcher_commands.py').read_text()
    name='launcher_commands_'+hashlib.sha256(source.encode()).hexdigest()
    if name not in sys.modules:
        module=types.ModuleType(name)
        exec(compile(source,name,'exec'),module.__dict__)
        sys.modules[name]=module
    return sys.modules[name]


CACHE_RUNTIME_SOURCE = ''


def load_cache_runtime():
    import types
    source = CACHE_RUNTIME_SOURCE or (Path(__file__).resolve().parent/'launcher_cache.py').read_text()
    name = 'launcher_cache_' + hashlib.sha256(source.encode()).hexdigest()
    if name not in sys.modules:
        module = types.ModuleType(name)
        exec(compile(source, name, 'exec'), module.__dict__)
        sys.modules[name] = module
    return sys.modules[name]


STREAM_RUNTIME_SOURCE = ''


def load_stream_runtime(root):
    """Materialize the launcher-bundled runtime at a stable, content-addressed path."""
    import importlib.util
    source=STREAM_RUNTIME_SOURCE
    if not source:source=(Path(__file__).resolve().parent/'launcher_streams.py').read_text()
    revision=hashlib.sha256(source.encode()).hexdigest();folder=Path(root)/'.stream-runtime'/revision
    folder.mkdir(mode=0o700,parents=True,exist_ok=True);path=folder/'launcher_streams.py'
    if path.exists():
        if hashlib.sha256(path.read_bytes()).hexdigest()!=revision:raise ValueError('Immutable streaming runtime changed')
    else:
        temporary=folder/'source.tmp';temporary.write_text(source);temporary.chmod(0o600);temporary.replace(path)
    name='launcher_streams_'+revision
    if name not in sys.modules:
        spec=importlib.util.spec_from_file_location(name,path);module=importlib.util.module_from_spec(spec);sys.modules[name]=module
        try:spec.loader.exec_module(module)
        except BaseException:sys.modules.pop(name,None);raise
    return sys.modules[name]


PERFORMANCE_RUNTIME_B64 = ''
PERFORMANCE_RUNTIME_DIRECTORY = None
PERFORMANCE_RUNTIME_LOCK = threading.Lock()


def load_performance_runtime():
    """Import the monitor shipped inside this atomically verified agent source."""
    global PERFORMANCE_RUNTIME_DIRECTORY
    with PERFORMANCE_RUNTIME_LOCK:
        if PERFORMANCE_RUNTIME_B64:
            if PERFORMANCE_RUNTIME_DIRECTORY is None:
                import base64
                PERFORMANCE_RUNTIME_DIRECTORY=tempfile.TemporaryDirectory(prefix='swarm-monitor-')
                archive=Path(PERFORMANCE_RUNTIME_DIRECTORY.name)/'runtime.zip'
                archive.write_bytes(base64.b64decode(PERFORMANCE_RUNTIME_B64,validate=True))
                sys.path.insert(0,str(archive))
        else:
            package=(Path(__file__).resolve().parents[2] if Path(__file__).resolve().parents[1].name in ('GenomeHarbor', 'JSwarm') else Path(__file__).resolve().parents[1])/'ramtop'
            if (package/'ramtop/telemetry.py').is_file() and str(package) not in sys.path:
                sys.path.insert(0,str(package))
        from ramtop.telemetry import PerformanceSampler, brief, valid_snapshot
        return PerformanceSampler,brief,valid_snapshot


class PerformanceCollector:
    def __init__(self):self.sampler=None

    def sample(self):
        # Prefer the native collector while it is publishing fresh host coverage.
        if Path('/.dockerenv').exists():
            try:
                state=json.loads((Path.home()/'.config/lifetech-launcher/performance-state.json').read_text())
                if state.get('status')=='published' and 0<=time.time()-state['sampled_at']<20:return []
            except (OSError,ValueError,KeyError,TypeError):pass
        if self.sampler is None:self.sampler=load_performance_runtime()[0]()
        return self.sampler.sample()

    def close(self):
        if self.sampler:self.sampler.close()


class LauncherTelemetry:
    """Independent local collectors and bounded, acknowledged five-minute replay."""
    def __init__(self, agent):
        self.agent=agent
        self.lock=threading.RLock()
        self.stopped=threading.Event()
        self.session=secrets.token_hex(16)
        self.sequence=0
        self.revision=0
        self.samples=collections.deque(maxlen=256)
        self.received={}
        self.latest={}
        self.threads=[]

    def record(self, kind, value, sampled_at=None):
        sampled_at=time.time() if sampled_at is None else sampled_at
        with self.lock:
            if kind=='performance':
                # Extended data rides the existing CPU envelope, so older enrolled
                # masters can relay it without a wire-protocol flag day.
                self.latest[kind]={'sampled_at':sampled_at,'value':copy.deepcopy(value)}
                return
            if kind=='cpu' and 'performance' in self.latest:
                value={**value,'performance':copy.deepcopy(self.latest['performance']['value'])}
            self.sequence+=1
            self.samples.append({'sequence':self.sequence,'kind':kind,'sampled_at':sampled_at,'value':copy.deepcopy(value)})
            self.latest[kind]={'sampled_at':sampled_at,'value':copy.deepcopy(value)}

    def pending(self, acknowledged):
        with self.lock:
            rows=[row for row in self.samples if row['sequence'] not in acknowledged and row['sampled_at']>=time.time()-295]
            # Prioritize each newest kind, then replay within both protocol limits.
            newest={row['kind']:row for row in rows}
            selected=[];seen=set()
            for row in list(newest.values())+rows:
                if row['sequence'] in seen:continue
                candidate={'session':self.session,'samples':selected+[row]}
                if len(selected)>=16 or len(json.dumps(candidate,allow_nan=False))>250000:continue
                selected.append(row);seen.add(row['sequence'])
            return {'session':self.session,'samples':copy.deepcopy(selected)}

    def accept(self, sender, args):
        session=args.get('session');rows=args.get('samples')
        if not isinstance(session,str) or not re.fullmatch('[a-f0-9]{32}',session):raise ValueError('Invalid telemetry session')
        if not isinstance(rows,list) or not 1<=len(rows)<=16 or len(json.dumps(args,allow_nan=False))>262144:raise ValueError('Invalid telemetry batch')
        now=time.time()
        for row in rows:
            if (not isinstance(row,dict) or type(row.get('sequence')) is not int or row['sequence']<1
                    or row.get('kind') not in ('cpu','hardware','disks','network_interfaces','performance')
                    or type(row.get('sampled_at')) not in (int,float) or not math.isfinite(row['sampled_at'])
                    or not now-300<=row['sampled_at']<=now+5):raise ValueError('Invalid telemetry sample')
            value=row.get('value')
            if row['kind']=='performance':
                if not load_performance_runtime()[2](value) or value['sampled_at']!=row['sampled_at']:
                    raise ValueError('Invalid performance telemetry')
            elif row['kind'] in ('disks','network_interfaces'):
                if not isinstance(value,list) or len(value)>32 or any(not isinstance(item,dict) for item in value):raise ValueError('Invalid telemetry rows')
            elif not isinstance(value,dict):raise ValueError('Invalid telemetry value')
            elif row['kind']=='hardware' and (not isinstance(value.get('gpus',[]),list) or len(value.get('gpus',[]))>16 or any(not isinstance(gpu,dict) for gpu in value.get('gpus',[]))):raise ValueError('Invalid GPU telemetry')
            elif row['kind']=='cpu' and (value.get('status') not in ('live','warming','permission_denied','unavailable')
                    or type(value.get('sampled_at')) not in (int,float) or value['sampled_at']!=row['sampled_at']):raise ValueError('Invalid CPU telemetry')
            if row['kind']=='cpu' and 'performance' in value and not load_performance_runtime()[2](value['performance']):
                raise ValueError('Invalid extended CPU telemetry')
            if row['kind']=='cpu' and value['status']=='live':
                if (type(value.get('cpus')) is not int or not 1<=value['cpus']<=65536
                        or type(value.get('percent')) not in (int,float) or not math.isfinite(value['percent'])
                        or not 0<=value['percent']<=100):raise ValueError('Invalid live CPU measurement')
        with self.lock:
            entry=self.received.setdefault(sender,{'events':collections.deque(maxlen=256),'seen':set(),'latest':{}})
            for row in rows:
                ident=(session,row['sequence'])
                if ident in entry['seen']:continue
                self.revision+=1
                event={**copy.deepcopy(row),'session':session,'revision':self.revision}
                entry['events'].append(event)
                before=entry['latest'].get(row['kind'])
                if before is None or row['sampled_at']>before['sampled_at']:entry['latest'][row['kind']]=event
                entry['seen'].add(ident)
            entry['events']=collections.deque((row for row in entry['events'] if row['sampled_at']>=now-300),maxlen=256)
            entry['seen']={(row['session'],row['sequence']) for row in entry['events']}
        return {'session':session,'sequences':[row['sequence'] for row in rows]}

    @staticmethod
    def host(name, latest):
        cpu=latest.get('cpu',{})
        value={'status':'warming',**cpu.get('value',{}),'host':name,'sampled_at':cpu.get('sampled_at',0)}
        for kind in ('hardware','disks','network_interfaces','performance'):
            if kind in latest:
                value[kind]=copy.deepcopy(latest[kind]['value'])
                if kind=='hardware':value[kind]['sampled_at']=latest[kind]['sampled_at']
        value['telemetry_transport']='launcher-push'
        return value

    def fleet(self, enrolled, after=0):
        now=time.time()
        # Published events are immutable. Snapshot references under the lock;
        # copying CPU arrays and rendering history must never block publishers.
        with self.lock:
            entries={name:{'latest':dict(entry['latest']),'events':list(entry['events'])}
                     for name,entry in self.received.items() if name in enrolled}
            revision=self.revision
        hosts=[self.host(name,entry['latest']) for name,entry in entries.items()]
        selected=sorted((row['revision'] for entry in entries.values() for row in entry['events']
                         if row['sampled_at']>=now-300 and row['revision']>after))[:128]
        selected=set(selected);events=[]
        for name,entry in entries.items():
            latest={}
            for row in sorted(entry['events'],key=lambda row:row['sampled_at']):
                # Compact each changed measurement once, before composing history.
                value=row['value']
                if row['kind']=='cpu':
                    value={k:v for k,v in value.items() if k not in ('logical_cpus','performance')}
                    if 'performance' in row['value']:
                        value['performance']=load_performance_runtime()[1](row['value']['performance'])
                elif row['kind']=='performance':value=load_performance_runtime()[1](value)
                elif row['kind']=='hardware':value={k:v for k,v in value.items() if k in ('gpus','sampled_at','stale')}
                latest[row['kind']]={**row,'value':value}
                if row['revision'] in selected:
                    events.append({'revision':row['revision'],'sampled_at':row['sampled_at'],
                                   'hosts':[self.host(name,latest)]})
        events.sort(key=lambda row:row['revision'])
        return {'hosts':hosts,'events':events,'session':self.session,
                'revision':events[-1]['revision'] if events else revision}

    def start(self):
        try:config=json.loads(self.agent.config.read_text())
        except (OSError,ValueError):config={}
        target={'local':True,'host':config.get('name','local')}
        interfaces=InterfaceSampler();disks=DiskSampler();gpu=GpuSampler();performance=PerformanceCollector()
        def hardware():
            value=gpu.sample(target,self.agent.hardware())
            value['sampled_at']=time.time()
            return value
        collectors={'cpu':self.agent.metrics_sampler.sample,'hardware':hardware,
                    'network_interfaces':lambda:interfaces.sample(target),'disks':lambda:disks.sample(target),
                    'performance':performance.sample}
        def run(kind, collect):
            import logging
            while not self.stopped.is_set():
                started=time.monotonic()
                try:
                    value=collect()
                    # A failed collector does not renew the previous measurement's timestamp.
                    if value!=[]:self.record(kind,value,value.get('sampled_at') if isinstance(value,dict) else None)
                except Exception as error:logging.debug('Telemetry %s failed: %s',kind,type(error).__name__)
                self.stopped.wait(max(.1,5-(time.monotonic()-started)))
            if kind=='performance':performance.close()
        for kind,collect in collectors.items():
            thread=threading.Thread(target=run,args=(kind,collect),name='telemetry-'+kind,daemon=True)
            thread.start();self.threads.append(thread)

    def snapshot(self, name):
        with self.lock:return self.host(name,self.latest)


RAM_CACHE_PROBE=r'''
import json,pathlib,re,stat,sys
root=pathlib.Path('/dev/shm');p=root/'.ready.json';expected=json.loads(sys.argv[1])
if not p.is_file() or p.is_symlink() or p.stat().st_size>65536:raise ValueError('RAM cache is not sealed')
v=json.loads(p.read_text())
if any(v.get(k)!=val for k,val in expected.items()):raise ValueError('RAM cache owner changed')
files=v.get('files');identities=v.get('identities')
if not isinstance(files,dict) or not files or len(files)>128 or not isinstance(identities,dict):raise ValueError('Invalid RAM cache manifest')
for name,row in files.items():
 if not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.-]{0,119}',name):raise ValueError('Invalid RAM cache filename')
 if type(row.get('bytes')) is not int or row['bytes']<1 or not re.fullmatch(r'[a-f0-9]{64}',str(row.get('sha256',''))):raise ValueError('Invalid RAM cache object')
 f=root/name;s=f.stat()
 if f.is_symlink() or not stat.S_ISREG(s.st_mode) or s.st_mode&0o222 or s.st_size!=row['bytes']:raise ValueError('RAM cache object is not sealed')
 if [s.st_dev,s.st_ino,s.st_size,s.st_mtime_ns]!=identities.get(name):raise ValueError('RAM cache object changed')
print(json.dumps(files))
'''


def verified_ram_caches():
    """Reuse sealed Docker tmpfs objects without re-reading their payloads."""
    def command(args):
        result=subprocess.run(['docker',*args],capture_output=True,text=True,timeout=15)
        if result.returncode:raise RuntimeError('RAM cache inventory unavailable')
        return result.stdout
    ids=command(['ps','-q','--filter','label=org.lifetech.cache-owner']).split()
    rows=json.loads(command(['inspect',*ids])) if ids else []
    caches=[];pending=[]
    for row in rows:
        labels=row.get('Config',{}).get('Labels') or {};key=labels.get('org.lifetech.cache-owner','')
        name=row.get('Name','').removeprefix('/')
        if (not re.fullmatch(r'[a-f0-9]{64}',key) or name!='swarm-job-'+key[:32]
                or labels.get('org.lifetech.group')!='shared-cache' or not row['State'].get('Running')):continue
        volume='swarm-cache-'+key[:32]
        if not any(m.get('Type')=='volume' and m.get('Name')==volume and m.get('Destination')=='/dev/shm' for m in row.get('Mounts',[])):continue
        try:
            mounted=json.loads(command(['volume','inspect',volume]))[0];options=mounted.get('Options') or {}
            if (mounted.get('Driver')!='local' or options.get('type')!='tmpfs' or options.get('device')!='tmpfs'
                    or (mounted.get('Labels') or {}).get('org.lifetech.cache-owner')!=key):continue
            expected={'key':key,'container_id':row['Id'],'started_at':row['State']['StartedAt']}
            files=json.loads(command(['exec',name,'python3','-c',RAM_CACHE_PROBE,json.dumps(expected)]))
        except (RuntimeError,ValueError,KeyError):
            pending.append(key);continue  # Filling caches take priority over optional prefetch.
        caches.append({'key':key,'container':name,'image':row['Image'],'files':files})
    return {'memory_domain':'host-ram','caches':caches,'pending_caches':pending}


def cache_cleanup_inventory():
    """Executed inside the existing command API; no filesystem payload reads."""
    import json
    import re
    import subprocess
    from collections import defaultdict
    owner_label = 'org.lifetech.cache-owner'

    def docker(*args):
        result = subprocess.run(['docker', *args], capture_output=True, text=True, timeout=30)
        if result.returncode:
            raise RuntimeError('Docker cache inventory failed: ' + result.stderr[-200:])
        return result.stdout

    def inspect(kind, ids):
        rows = []
        for start in range(0, len(ids), 100):
            rows.extend(json.loads(docker(*kind, *ids[start:start + 100])))
        return rows

    ids = docker('ps', '-aq').split()
    if len(ids) > 4096:
        raise ValueError('Container inventory exceeds bounded cleanup limit')
    containers = inspect(['inspect'], ids)
    consumers = defaultdict(list)
    owners = {}
    for row in containers:
        for mount in row.get('Mounts', []):
            if mount.get('Type') == 'volume':
                consumers[mount['Name']].append(row['Name'].lstrip('/'))
        labels = row.get('Config', {}).get('Labels') or {}
        key = labels.get(owner_label, '')
        if (re.fullmatch(r'[a-f0-9]{64}', key) and row['Name'].lstrip('/') == 'swarm-job-' + key[:32]
                and labels.get('org.lifetech.group') == 'shared-cache'):
            owners[key] = row
    names = docker('volume', 'ls', '-q', '--filter', 'label=' + owner_label).split()
    if len(names) > 4096:
        raise ValueError('Volume inventory exceeds bounded cleanup limit')
    volumes = {}
    for volume in inspect(['volume', 'inspect'], names):
        key = (volume.get('Labels') or {}).get(owner_label, '')
        options = volume.get('Options') or {}
        if (re.fullmatch(r'[a-f0-9]{64}', key) and volume['Name'] == 'swarm-cache-' + key[:32]
                and volume.get('Driver') == 'local' and options.get('type') == 'tmpfs' and options.get('device') == 'tmpfs'):
            volumes[key] = volume
    result = []
    for key in sorted(set(owners) | set(volumes)):
        owner = owners.get(key)
        name, volume = 'swarm-job-' + key[:32], 'swarm-cache-' + key[:32]
        result.append({'key': key, 'container': name, 'volume': volume,
                       'owner_id': owner['Id'] if owner else None,
                       'owner_state': owner.get('State', {}).get('Status') if owner else 'missing',
                       'created': owner.get('Created', '') if owner else '',
                       'volume_verified': key in volumes,
                       'owner_verified': owner is None or any(m.get('Type')=='volume' and m.get('Name')==volume and m.get('Destination')=='/dev/shm' for m in owner.get('Mounts',[])),
                       'consumers': [consumer for consumer in consumers[volume] if owner is None or consumer != name],
                       'exec_readers': len(owner.get('ExecIDs') or []) if owner else 0})
    return result

def _cache_cleanup_docker(args,missing=False):
    result=subprocess.run(['docker',*args],capture_output=True,text=True,timeout=30)
    if result.returncode:
        error=result.stderr.lower()
        if missing and ('no such' in error or 'not found' in error):return None
        raise RuntimeError('Cache cleanup Docker operation failed: '+result.stderr[-250:])
    return result.stdout


def _cache_cleanup_inspect(kind,name):
    raw=_cache_cleanup_docker([kind,'inspect',name],missing=True)
    return json.loads(raw)[0] if raw is not None else None


def cleanup_ram_cache(args):
    """Controller attests terminal-only ownership; this node fences live readers.

    Queue bindings live on the controller, not here. This signed operation must
    be called only after a fresh queue check, one exact owner instance at a time.
    No implicit discovery, arbitrary names, prune, or force-volume removal.
    """
    if (not isinstance(args,dict) or set(args)!={'key','owner_id','machine','ownership','dry_run'}
            or not isinstance(args.get('key'),str) or not re.fullmatch(r'[a-f0-9]{64}',args['key'])
            or args.get('owner_id') is not None and (not isinstance(args['owner_id'],str) or not re.fullmatch(r'[a-f0-9]{64}',args['owner_id']))
            or not isinstance(args.get('machine'),str) or not re.fullmatch(r'[a-f0-9]{64}',args['machine'])
            or args.get('ownership')!='terminal-only' or type(args.get('dry_run')) is not bool):
        raise ValueError('Expected one exact cache identity and controller terminal-ownership attestation')
    if hashlib.sha256(Path('/etc/machine-id').read_bytes()).hexdigest()!=args['machine']:
        raise ValueError('Cache cleanup machine identity differs')
    key=args['key'];name='swarm-job-'+key[:32];volume_name='swarm-cache-'+key[:32]
    result={'key':key,'removed':False,'owner_removed':False,'dry_run':args['dry_run']}
    def skip(reason):return {**result,'reason':reason}
    def valid_owner(row):
        labels=row.get('Config',{}).get('Labels') or {}
        return (row.get('Name','').lstrip('/')==name and labels.get('org.lifetech.cache-owner')==key
                and labels.get('org.lifetech.group')=='shared-cache'
                and any(m.get('Type')=='volume' and m.get('Name')==volume_name and m.get('Destination')=='/dev/shm' for m in row.get('Mounts',[])))
    def valid_volume(row):
        options=row.get('Options') or {}
        return (row.get('Name')==volume_name and row.get('Driver')=='local'
                and options.get('type')=='tmpfs' and options.get('device')=='tmpfs'
                and (row.get('Labels') or {}).get('org.lifetech.cache-owner')==key)
    owner=_cache_cleanup_inspect('container',name)
    if (owner['Id'] if owner else None)!=args['owner_id']:return skip('owner_changed')
    if owner and not valid_owner(owner):return skip('owner_identity_invalid')
    volume=_cache_cleanup_inspect('volume',volume_name)
    if not volume:return skip('volume_missing')
    if not valid_volume(volume):return skip('volume_identity_invalid')
    consumers=_cache_cleanup_docker(['ps','-a','--filter','volume='+volume_name,'--format','{{.Names}}']).splitlines()
    if any(consumer!=name for consumer in consumers):return skip('docker_consumer')
    # Recheck the exact owner after consumer enumeration. A new loader under
    # the same content key is a different instance and cannot be removed.
    owner=_cache_cleanup_inspect('container',name)
    if (owner['Id'] if owner else None)!=args['owner_id']:return skip('owner_changed')
    if owner and (not valid_owner(owner) or owner.get('ExecIDs')):
        return skip('active_transfer_or_owner_changed')
    if args['dry_run']:return skip('eligible_terminal_cache')
    if owner:
        _cache_cleanup_docker(['rm','-f',owner['Id']])
        result['owner_removed']=True
    # An ownerless volume never authorizes deleting any subsequently created
    # owner. Docker's non-forced reference check protects a new attachment.
    volume=_cache_cleanup_inspect('volume',volume_name)
    if not volume:return {**result,'removed':True,'reason':'already_removed'}
    if not valid_volume(volume):return skip('volume_identity_changed')
    try:_cache_cleanup_docker(['volume','rm',volume_name])
    except RuntimeError as error:
        if 'in use' in str(error).lower() or 'being used' in str(error).lower():return skip('docker_consumer')
        raise
    return {**result,'removed':True,'reason':'removed'}


class DockerWorkerJournal:
    """Durable attempt identity and last observation, independent of agent lifetime."""
    def __init__(self,path,accounting_reader=None):
        import sqlite3
        self.accounting_reader=accounting_reader
        self.path=Path(path);self.path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
        with sqlite3.connect(self.path,timeout=15) as db:
            db.execute('PRAGMA journal_mode=WAL');db.execute('PRAGMA synchronous=FULL')
            db.execute('CREATE TABLE IF NOT EXISTS workers (job TEXT PRIMARY KEY, container_id TEXT UNIQUE NOT NULL, observation TEXT NOT NULL, updated REAL NOT NULL)')
        self.path.chmod(0o600)
        if os.geteuid()==0:os.chown(self.path,self.path.parent.stat().st_uid,self.path.parent.stat().st_gid)

    def record(self,item):
        import sqlite3
        labels=item.get('Config',{}).get('Labels') or {}
        job=labels.get('org.lifetech.job')
        if labels.get('org.lifetech.worker')!='true' or not isinstance(job,str) or not re.fullmatch(r'[a-f0-9]{32}',job):
            raise ValueError('Docker worker ownership labels missing or invalid')
        state=item['State'];limits=item['HostConfig'];name=item['Name'].removeprefix('/')
        value={'job':job,'container':name,'container_id':item['Id'],'run':labels.get('org.lifetech.run'),
               'image':item['Image'],'controller_report':labels.get('org.lifetech.report'),'agent_revision':labels.get('org.lifetech.agent-revision'),
               'group':labels.get('org.lifetech.group'),'sink':labels.get('org.lifetech.sink'),
               'gpu_allocation':labels.get('org.lifetech.accelerator'),
               'cpus':limits.get('NanoCpus',0)/1e9,'memory_mb':limits.get('Memory',0)//1024**2,
               'status':state['Status'],'running':bool(state.get('Running') or state.get('Restarting')),
               'exit_code':state.get('ExitCode') if state['Status'] in ('exited','dead') else None,
               'started_at':state.get('StartedAt'),'finished_at':state.get('FinishedAt') if state['Status'] in ('exited','dead') else None,
               'oom_killed':state.get('OOMKilled',False),'observed_at':time.time()}
        sample=_WORKER_CPU_CACHE.get(item['Id']) or _WORKER_CPU_CACHE.get(name) or {}
        if type(sample.get('total')) in (int,float) and sample['total']>=0:
            value.update(cpu_core_seconds=sample['total']/1e9,cpu_sampled_at=sample.get('sampled_at',time.time()-max(0,time.monotonic()-sample.get('time',time.monotonic()))),cpu_accounting_coverage='sampled-partial',cpu_measurement_basis='Last observed Docker CPU total; terminal counter unavailable, not a complete execution measurement')
        memory_fields=('memory_current_bytes','memory_peak_bytes','memory_anon_bytes','memory_file_bytes',
                       'memory_shmem_bytes','memory_swap_bytes','memory_sampled_at','memory_measurement_basis','memory_accounting_coverage')
        ram=_WORKER_RAM_CACHE.get(item['Id']) or _WORKER_RAM_CACHE.get(name) or {}
        if type(ram.get('raw_bytes')) is int and ram['raw_bytes']>=0:
            value.update(memory_current_bytes=ram['raw_bytes'],memory_peak_bytes=ram.get('raw_peak_bytes',ram['raw_bytes']),
                         memory_sampled_at=ram.get('sampled_at',value['observed_at']),memory_accounting_coverage='sampled-partial',
                         memory_measurement_basis='Last observed Docker raw RAM including file and shmem; sampled peak may miss between-sample usage')
        if state['Status'] in ('exited','dead') and self.accounting_reader:
            accounting=self.accounting_reader(item)
            for key,amount in accounting.items():
                family='memory' if key.startswith('memory_') else 'cpu' if key.startswith('cpu_') else None
                if family and accounting.get(family+'_accounting_coverage')=='unavailable' and family+'_accounting_coverage' in value:continue
                value[key]=amount
        try:
            import datetime
            start=datetime.datetime.fromisoformat(value['started_at'].replace('Z','+00:00')).timestamp()
            end=datetime.datetime.fromisoformat(value['finished_at'].replace('Z','+00:00')).timestamp() if value['finished_at'] else None
            if start>0 and end is not None and end>=start:value.update(started=value['started_at'],finished=value['finished_at'],elapsed_seconds=end-start)
        except (ValueError,TypeError):pass
        with sqlite3.connect(self.path,timeout=15) as db:
            db.execute('BEGIN IMMEDIATE')
            previous=db.execute('SELECT container_id,observation FROM workers WHERE job=?',(job,)).fetchone()
            if previous and previous[0]!=item['Id']:raise ValueError('Docker job identity was reused by a different container')
            if previous:
                durable=json.loads(previous[1])
                if 'cpu_core_seconds' not in value and 'cpu_core_seconds' in durable:
                    for key in ('cpu_core_seconds','cpu_throttled_seconds','cpu_sampled_at','cpu_measurement_basis','cpu_accounting_coverage'):
                        if key in durable:value[key]=durable[key]
                # Never discard a terminal pre-exit receipt when later cleanup
                # makes its metadata unavailable. Identity is fenced above.
                if durable.get('cpu_accounting_coverage')=='final-pre-exit' and value.get('cpu_accounting_coverage')!='final-pre-exit':
                    for key in ('cpu_core_seconds','cpu_throttled_seconds','cpu_sampled_at','cpu_measurement_basis','cpu_accounting_coverage','elapsed_seconds','started','finished'):
                        if key in durable:value[key]=durable[key]
                if ('memory_peak_bytes' not in value or
                    durable.get('memory_accounting_coverage')=='final-pre-exit' and value.get('memory_accounting_coverage')!='final-pre-exit'):
                    for key in memory_fields:
                        if key in durable:value[key]=durable[key]
                elif value.get('memory_accounting_coverage')=='sampled-partial':
                    value['memory_peak_bytes']=max(value['memory_peak_bytes'],durable.get('memory_peak_bytes',0))
                # Poll freshness is volatile. Commit attempts and state changes,
                # rather than fsyncing an unchanged worker on every status read.
                if {k:v for k,v in durable.items() if k!='observed_at'}=={k:v for k,v in value.items() if k!='observed_at'}:
                    return value
            db.execute('INSERT INTO workers VALUES(?,?,?,?) ON CONFLICT(job) DO UPDATE SET observation=excluded.observation,updated=excluded.updated',
                       (job,item['Id'],json.dumps(value),value['observed_at']))
        return value

    def finish_event(self,event):
        """Retain Docker's exit receipt even when cleanup wins the inspect race."""
        import sqlite3
        if event.get('Action',event.get('status'))!='die':return
        actor=event.get('Actor') or {};labels=actor.get('Attributes') or {};job=labels.get('org.lifetech.job')
        if labels.get('org.lifetech.worker')!='true' or not re.fullmatch(r'[a-f0-9]{32}',str(job or '')):return
        try:code=int(labels['exitCode'])
        except (KeyError,TypeError,ValueError):return
        with sqlite3.connect(self.path,timeout=15) as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT container_id,observation FROM workers WHERE job=?',(job,)).fetchone()
            if not row:return  # A receipt cannot invent a missing attempt's provenance.
            if row[0]!=actor.get('ID'):raise ValueError('Docker event container identity differs')
            import datetime
            ended=event.get('timeNano',event['time']*1_000_000_000)/1_000_000_000
            finished=datetime.datetime.fromtimestamp(ended,datetime.timezone.utc).isoformat()
            value=json.loads(row[1]);value.update(status='exited',running=False,exit_code=code,finished_at=finished,observed_at=time.time())
            try:
                started=datetime.datetime.fromisoformat(value['started_at'].replace('Z','+00:00')).timestamp()
                if started>0 and ended>=started:value.update(started=value['started_at'],finished=finished,elapsed_seconds=ended-started)
            except (ValueError,TypeError,KeyError):pass
            db.execute('UPDATE workers SET observation=?,updated=? WHERE job=?',(json.dumps(value),value['observed_at'],job))

    def get(self,job):
        import sqlite3
        with sqlite3.connect(self.path,timeout=15) as db:
            row=db.execute('SELECT observation FROM workers WHERE job=?',(job,)).fetchone()
        return json.loads(row[0]) if row else None

    def records(self,after='',limit=100):
        import sqlite3
        with sqlite3.connect(self.path,timeout=15) as db:
            rows=db.execute('SELECT observation FROM workers WHERE job>? ORDER BY job LIMIT ?',(after,limit)).fetchall()
        return [json.loads(row[0]) for row in rows]


class Agent:
    def __init__(self, config, root):
        self.config = config
        self.instance_lock=launcher_lock(Path(config).parent/'launcher-agent.lock')
        folder=Path(__file__).resolve().parent
        self.source_revision=hashlib.sha256(b''.join((folder/name).read_bytes() for name in ('launcher_peers.py','launcher_discovery.py','launcher_registration.py','launcher_agent.py','launcher_registry.py'))).hexdigest()
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.lock = threading.RLock()
        self.changed = threading.Condition(self.lock)
        self.nonce_lock = threading.Lock()
        self.nonces = {}
        self.metrics_sampler=ResourceSampler()
        self.telemetry=LauncherTelemetry(self)
        self.gpu_lock=threading.Lock()
        self.gpu_validation=None
        self.hardware_cache=None
        self.hardware_sampled=0
        self.jobs = {}
        self.active_workers=set()
        self.storage_status={}
        self.storage_lock=threading.RLock()
        self.storage_stop=threading.Event()
        self.resource_condition=threading.Condition();self.resource_revision=0
        self.nvidia_sampler=NvidiaSampler(self.gpu_inventory_changed)
        self.uploads = {}
        self.registry_transfers=0
        self.image_progress={}
        self.browsers=set()
        self.browser_timers={}
        self.pending_resources={}
        stream_root=self.root
        stream_directory=json.loads(self.config.read_text()).get('stream_state_directory')
        if stream_directory is not None:
            if not isinstance(stream_directory,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,80}',stream_directory):raise ValueError('Invalid persistent streaming state directory')
            stream_root=self.config.parent/stream_directory
            stream_root.mkdir(mode=0o700,parents=True,exist_ok=True)
        self.commands=load_command_runtime().CommandProxy(self,COMMAND_RUNTIME_SOURCE or (Path(__file__).resolve().parent/'launcher_commands.py').read_text())
        self.stream_work=load_stream_runtime(stream_root).Engine(self,ResourceLease,worker_policy,state_root=stream_root)
        from concurrent.futures import ThreadPoolExecutor
        self.control_executor=ThreadPoolExecutor(max_workers=max(2,min(4,os.cpu_count() or 2)),thread_name_prefix='launcher-control')
        directory=Path(os.getenv('SWARM_RESOURCE_DIR',str(self.root/'.resources')))
        directory.mkdir(parents=True,exist_ok=True,mode=0o700);(directory/'docker-workers').touch()
        self.worker_journal=DockerWorkerJournal(self.config.parent/'docker-worker-journal.sqlite',self.stream_work.read_worker_accounting)
        self.recover_docker_workers()
        self.recover_pending_resources()
        self.busy()
        for path in self.root.glob('*.upload'):path.unlink()
        for path in self.root.glob('*-p2p/browser.json'):
            self.remove_containers(path.parent.name.removesuffix('-p2p'))
            shutil.rmtree(path.parent)
        # A process restart must never pretend that a previous job is still running.
        for path in self.root.glob('*/state.json'):
            if path.parent.is_symlink() or path.is_symlink():continue
            state = json.loads(path.read_text())
            if state.get('status') in ('running', 'starting'):
                (path.parent/'STOP').touch()
                state.update(status='interrupted', error='Agent restarted during execution')
                path.write_text(json.dumps(state))
                self.remove_containers(path.parent.name)
        threading.Thread(target=self.maintain_storage,daemon=True).start()
        self.worker_event_process=None
        self.worker_event_thread=threading.Thread(target=self.watch_docker_workers,daemon=True,name='docker-worker-journal')
        self.worker_event_thread.start()
        self.telemetry.start()

    def cleanup_workspaces(self):
        """Fail closed if container ownership cannot be inspected."""
        import docker
        try:
            with self.storage_lock:
                with contextlib.closing(docker.from_env(timeout=5)) as client:
                    mounts=[mount['Source'] for container in client.containers.list(all=True)
                            for mount in container.attrs.get('Mounts',[]) if mount.get('Type')=='bind']
                for proc in Path('/proc').glob('[0-9]*'):
                    try:mounts.append(str((proc/'cwd').resolve(strict=True)))
                    except OSError:pass
                with self.lock:
                    active=set(self.active_workers)|{key for key,value in self.jobs.items() if value['status'] in ('running','starting')}
                summary=prune_workspaces(self.root,active,mounts)
                with self.lock:
                    for ident in summary['removed']:
                        if ident in self.jobs:self.jobs[ident]['workspace_retained']=False
                    idle=not self.active_workers
                    finished=[key for key,value in self.jobs.items() if key not in self.active_workers and value['status'] not in ('running','starting')]
                    for key in finished[:-10]:
                        if not (self.folder(key)/'source').exists():del self.jobs[key]
                if idle:summary['package_cache_bytes']=prune_package_cache(self.root/'home/.cache/organization-tests/packages',mounts)
                self.storage_status=summary
                return summary
        except Exception as error:
            self.storage_status={'error':type(error).__name__+': '+str(error)[:200]}
            return self.storage_status

    def maintain_storage(self):
        while not self.storage_stop.is_set():
            self.cleanup_workspaces()
            self.storage_stop.wait(300)

    def busy(self):
        path=Path('/tmp/launcher-agent-busy.json');temporary=path.with_suffix('.tmp')
        with self.lock:
            work={'jobs':[key for key,job in self.jobs.items() if job['status']=='running'],
                  'browsers':sorted(self.browsers),'transfers':self.registry_transfers+len(self.uploads)+len(getattr(self,'pending_resources',{}))+(self.commands.active() if hasattr(self,'commands') else 0)}
            # Older supervisors read these keys as replacement blockers. No
            # workload is a blocker; preserve counts only as observations.
            temporary.write_text(json.dumps({'jobs':[],'browsers':[],'transfers':0,'observed_work':work,
                'workers_survive_restart':True,'time':time.time()}));temporary.replace(path)


    @staticmethod
    def image_fingerprint(image):
        metadata=image.attrs
        content={key:metadata[key] for key in ('RootFS','Config','Architecture','Os','Created')}
        content['Config']={key:value for key,value in content['Config'].items() if value is not None}
        for name in ('AttachStdin','AttachStdout','AttachStderr','StdinOnce','Tty','OpenStdin'):
            content['Config'][name]=content['Config'].get(name) or False
        for name in ('WorkingDir','User','Image','Domainname','Hostname'):
            content['Config'][name]=content['Config'].get(name) or ''
        return hashlib.sha256(json.dumps(content,sort_keys=True,separators=(',',':')).encode()).hexdigest()

    def client(self):
        import docker
        return contextlib.closing(docker.from_env(timeout=120))

    @staticmethod
    def remove_container(container):
        """Accept a concurrent Docker removal without masking other failures."""
        from docker.errors import APIError, NotFound
        try:
            container.remove(force=True)
        except NotFound:
            pass
        except APIError as error:
            if error.status_code != 409 or not re.fullmatch(
                r'removal of container [a-f0-9]{12,64} is already in progress',
                error.explanation or '',
            ):
                raise

    def remove_containers(self, ident):
        with self.client() as client:
            for container in client.containers.list(all=True, filters={'name':'orgportal-test-'+ident+'-'}):
                if container.name.startswith('orgportal-test-'+ident+'-'):
                    self.remove_container(container)

    async def handle(self, request):
        import asyncio
        from aiohttp import web
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        try:
            return web.Response(body=await self.process(await request.read()),content_type='application/octet-stream')
        except ValueError:
            raise web.HTTPForbidden(text='Agent authentication rejected')

    async def process(self, raw):
        import asyncio
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        config = json.loads(self.config.read_text())
        cipher = AESGCM(bytes.fromhex(config['key']))
        try:
            value = json.loads(cipher.decrypt(raw[:12], raw[12:], VERSION.encode()))
            if (value.get('to') != config['name'] or value.get('from') not in config['peers'] or
                    abs(time.time()-value['time']) > 60 or not re.fullmatch('[a-f0-9]{32}',value['nonce'])):
                raise ValueError('Invalid request identity or freshness')
            verify_controller(value,config)
            # Authentication must not block the event loop on Docker/job I/O.
            # Command frames have their own idempotent IDs, input offsets and
            # output acknowledgements. Their high-rate polling must not exhaust
            # the replay cache used by non-idempotent control mutations. Fresh,
            # authenticated stream observations cannot mutate accepted work.
            if value['op'] not in ('command.start','command.poll','command.close','stream.status','stream.list','stream.capabilities'):
                with self.nonce_lock:
                    import heapq
                    now=time.time()
                    if not hasattr(self,'nonce_expirations'):
                        self.nonce_expirations=[(expiry,nonce) for nonce,expiry in self.nonces.items()]
                        heapq.heapify(self.nonce_expirations)
                    while self.nonce_expirations and self.nonce_expirations[0][0]<=now:
                        expiry,nonce=heapq.heappop(self.nonce_expirations)
                        if self.nonces.get(nonce)==expiry:self.nonces.pop(nonce)
                    if value['nonce'] in self.nonces:
                        raise ValueError('Replayed request or request limit')
                    expiry=now+120
                    self.nonces[value['nonce']]=expiry
                    heapq.heappush(self.nonce_expirations,(expiry,value['nonce']))
            try:
                if value['op']=='metrics.publish':
                    if config['name']!=config.get('master','compai.local'):raise ValueError('Telemetry receiver is not the master')
                    result=await asyncio.to_thread(self.telemetry.accept,value['from'],value.get('args',{}))
                    result['policy']=self.telemetry_policy()
                else:
                    executor=getattr(self,'control_executor',None)
                    if executor is not None and value['op'] in ('worker.policy','stream.status','stream.list','stream.capabilities'):
                        result=await asyncio.get_running_loop().run_in_executor(executor,self.dispatch,value['op'],value.get('args',{}))
                    else:result = await asyncio.to_thread(self.dispatch, value['op'], value.get('args',{}))
                reply = {'ok':True, 'result':result}
            except Exception as error:
                reply = {'ok':False,'error':type(error).__name__+': '+str(error)[:800]}
            reply.update(nonce=value['nonce'], node=config['name'], time=time.time())
            nonce = secrets.token_bytes(12)
            return nonce+cipher.encrypt(nonce,json.dumps(reply).encode(),VERSION.encode())
        except Exception as error:
            import logging
            reason=str(error)
            if reason not in {'Controller signature rejected','Execution requires the enrolled controller',
                              'Invalid request identity or freshness','Replayed request or request limit'}:
                reason='Invalid encrypted request or response'
            logging.warning('Agent RPC rejected: %s; replay entries=%d',reason,len(self.nonces))
            raise ValueError('Agent authentication rejected') from None

    def folder(self, ident):
        if not isinstance(ident,str) or not re.fullmatch(r'[0-9TZ-]+-[a-f0-9]{8}(?:-p2p)?',ident):
            raise ValueError('Invalid run ID')
        return self.root/ident

    def resource_changed(self):
        condition=self.__dict__.setdefault('resource_condition',threading.Condition())
        with condition:
            self.resource_revision=getattr(self,'resource_revision',0)+1;condition.notify_all()

    def gpu_inventory_changed(self):
        self.gpu_resource_cache=None
        self.resource_changed()

    def watch_docker_workers(self):
        """Observe existing Docker identities; this pipe never owns a worker."""
        since=str(int(time.time())-2)
        while not self.storage_stop.is_set():
            process=None
            try:
                process=subprocess.Popen(['docker','events','--since',since,'--filter','type=container',
                    '--filter','label=org.lifetech.worker=true','--format','{{json .}}'],stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,text=True)
                self.worker_event_process=process
                for line in process.stdout:
                    if self.storage_stop.is_set():break
                    event=json.loads(line);action=event.get('Action',event.get('status'))
                    if action in ('create','start','die'):
                        self.resource_changed()
                        actor=event.get('Actor') or {};attributes=actor.get('Attributes') or {}
                        if attributes.get('org.lifetech.group') in ('stream-work','shared-cache'):continue
                        job=attributes.get('org.lifetech.job','')
                        if re.fullmatch(r'[a-f0-9]{32}',job):
                            inspected=subprocess.run(['docker','inspect',actor['ID']],capture_output=True,text=True,timeout=15)
                            if inspected.returncode==0:
                                for item in json.loads(inspected.stdout):self.worker_journal.record(item)
                            self.worker_journal.finish_event(event)
                    if type(event.get('time')) is int:since=str(event['time'])
            except (OSError,ValueError,KeyError,subprocess.SubprocessError):pass
            finally:
                if process and process.poll() is None:
                    process.terminate()
                    try:process.wait(timeout=3)
                    except subprocess.TimeoutExpired:process.kill();process.wait()
            self.storage_stop.wait(1)

    def recover_pending_resources(self):
        """Adopt accepted launch reservations under their existing job IDs."""
        directory=Path(os.getenv('SWARM_RESOURCE_DIR',str(self.root/'.resources')))
        with (directory/'pool.lock').open('a') as guard:
            fcntl.flock(guard,fcntl.LOCK_EX)
            for path in directory.glob('*.json'):
                file=path.open('r+')
                try:
                    fcntl.flock(file,fcntl.LOCK_EX|fcntl.LOCK_NB);row=json.load(file)
                    job=row.get('job')
                    if not re.fullmatch(r'[a-f0-9]{32}',str(job or '')) or row.get('handover_until',0)<=time.time():file.close();continue
                    lease=ResourceLease.__new__(ResourceLease);lease.directory=directory;lease.path=path
                    lease.file=file;lease.key=row['id'];lease.row=row;lease.limits=None
                    row['handover_until']=min(row['handover_until'],time.time()+LAUNCH_RESERVATION_SECONDS);lease.write()
                    self.pending_resources[job]={'lease':lease,'deadline':time.monotonic()+row['handover_until']-time.time()}
                except (BlockingIOError,ValueError,KeyError):file.close()

    def recover_docker_workers(self):
        """Reconcile before readiness; never stop or recreate a Docker-owned shard."""
        result=subprocess.run(['docker','ps','-aq','--filter','label=org.lifetech.worker=true'],capture_output=True,text=True,timeout=15)
        if result.returncode:raise RuntimeError('Cannot reconcile Docker workers before agent readiness')
        ids=result.stdout.split()
        for offset in range(0,len(ids),100):
            inspected=subprocess.run(['docker','inspect',*ids[offset:offset+100]],capture_output=True,text=True,timeout=20)
            # A controller may remove a completed container during startup.
            if inspected.returncode and 'no such' not in inspected.stderr.lower():raise RuntimeError('Docker worker reconciliation failed')
            for item in json.loads(inspected.stdout or '[]'):
                labels=item.get('Config',{}).get('Labels') or {}
                # Streaming retries and reusable caches have their own durable
                # journals; their deterministic names may legitimately recur.
                if labels.get('org.lifetech.group') in ('stream-work','shared-cache'):continue
                job=labels.get('org.lifetech.job','')
                if re.fullmatch(r'[a-f0-9]{32}',job):
                    previous=self.worker_journal.get(job)
                    # Historical stopped containers cannot replace a journaled attempt.
                    # Live identity collisions still fail reconciliation explicitly.
                    state=item.get('State',{})
                    if (previous and previous['container_id']!=item['Id']
                            and state.get('Status') in ('exited','dead')
                            and not state.get('Running') and not state.get('Restarting')):continue
                    self.worker_journal.record(item)

    def worker_status(self,args):
        if set(args)!={'job','container'} or not re.fullmatch(r'[a-f0-9]{32}',str(args.get('job',''))) or not re.fullmatch(r'(?:genomics|swarm-job)-[a-f0-9]{32}',str(args.get('container',''))):
            raise ValueError('Expected a Docker job identity and container name')
        previous=self.worker_journal.get(args['job'])
        if previous and previous['container']!=args['container']:raise ValueError('Docker job container name differs')
        inspected=subprocess.run(['docker','inspect',args['container']],capture_output=True,text=True,timeout=15)
        if inspected.returncode:
            if 'no such object:' in inspected.stderr.lower() or 'no such container:' in inspected.stderr.lower():
                return {'found':False,'last_observation':previous}
            raise RuntimeError('Docker worker inspection unavailable')
        item=json.loads(inspected.stdout)[0]
        if item.get('Name','').removeprefix('/')!=args['container'] or (item.get('Config',{}).get('Labels') or {}).get('org.lifetech.job')!=args['job']:
            raise ValueError('Docker worker ownership differs')
        return {'found':True,**self.worker_journal.record(item)}

    def reconcile_resources(self):
        workers=docker_workers()
        with self.lock:
            running={r['container'] for r in workers}
            changed=False
            for ident,entry in list(self.pending_resources.items()):
                if entry['lease'].row['container'] in running or time.monotonic()>=entry['deadline']:
                    entry['lease'].close();del self.pending_resources[ident];changed=True
            if changed:self.busy()
            return workers

    def expire_resource(self,ident,token=None):
        # Explicit owner cancellation only; workers never expire on agent exit.
        with self.lock:
            workers=self.reconcile_resources()
            for row in workers:
                if row['job']==ident:
                    subprocess.run(['docker','rm','-f',row['id']],check=True,capture_output=True,timeout=20)
            entry=self.pending_resources.pop(ident,None)
            if entry:entry['lease'].close()
            self.busy()

    def local_jobs(self):
        """Only work owned by this worker; deployment containers are excluded."""
        with self.lock:
            rows=[{'id':ident,'kind':'test','status':'stopping' if (self.folder(ident)/'STOP').exists() else 'running',
                   'label':'Source preparation' if job.get('prepare_only') else 'Test job'}
                  for ident,job in self.jobs.items() if job['status'] in ('running','starting')]
            streams=self.stream_work.store.list(states=('queued','admitting','launching','running','retry_wait','stopping'),limit=100) if hasattr(self,'stream_work') else []
            stream_containers={r['container'] for r in streams if r['container']}
            rows.extend({'id':r['id'],'kind':'stream','status':'stopping' if r['state']=='stopping' else 'running' if r['state'] in ('launching','running') else 'waiting','label':r['spec'].get('label',r['spec']['recipe'])} for r in streams)
            rows.extend({'id':r['job'],'kind':'resource','status':'running','label':r['group']} for r in self.reconcile_resources() if r['container'] not in stream_containers)
            rows.extend({'id':ident,'kind':'resource','status':'waiting','label':entry['lease'].row['group']} for ident,entry in self.pending_resources.items())
            rows.extend({'id':ident,'kind':'browser','status':'running','label':'Swarm browser'} for ident in self.browsers)
            external = local_peer_qc(self.config)
            if external:
                rows.append(external)
            return rows

    def stop_local_job(self, kind, ident):
        if not isinstance(ident,str) or kind not in ('test','resource','browser','peer-qc','stream'):
            raise ValueError('Invalid local job')
        with self.lock:
            if not any(row['id']==ident and row['kind']==kind for row in self.local_jobs()):
                raise ValueError('Job is no longer active on this computer')
            if kind=='stream':
                self.stream_work.dispatch('stream.cancel',{'id':ident})
            elif kind=='peer-qc':
                subprocess.run(['systemctl', '--user', 'stop', 'venter-peer-qc.service'], check=True, timeout=45)
            elif kind=='resource':
                self.expire_resource(ident)
            else:
                self.dispatch('job.stop' if kind=='test' else 'p2p.stop',{'id':ident})
        return {'id':ident,'kind':kind,'status':'stopping'}

    def telemetry_policy(self):
        try:policy=json.loads((self.config.parent/'telemetry-transport.json').read_text())
        except FileNotFoundError:return {'transport':'tcp','updated_at':0}
        if policy.get('transport') not in ('tcp','webrtc'):raise ValueError('Invalid telemetry transport policy')
        return policy

    def save_telemetry_policy(self, policy):
        if not isinstance(policy,dict) or policy.get('transport') not in ('tcp','webrtc'):raise ValueError('Invalid telemetry transport policy')
        with self.lock:
            path=self.config.parent/'telemetry-transport.json'
            temporary=path.with_suffix('.tmp')
            temporary.write_text(json.dumps(policy));temporary.chmod(0o600);temporary.replace(path)
        return policy

    def worker_storage_policy(self):
        return self.stream_work.storage_policy()

    def validate_worker_storage(self, paths):
        if self.worker_storage_policy()['mode']!='ram-only':return {'validated':True}
        return self.commands.dispatch('command.storage',{'id':secrets.token_hex(16),'paths':paths})

    def validate_worker_command(self, argv):
        """Apply node policy to managed workers launched by older controllers too."""
        if argv[:2]!=['docker','run'] or not any(a in ('org.lifetech.worker=true','--label=org.lifetech.worker=true') for a in argv):return
        if self.worker_storage_policy()['mode']!='ram-only':return
        if '--read-only' not in argv:raise ValueError('RAM-only workers require a read-only container root')
        def option(name):
            for index,arg in enumerate(argv):
                if arg==name and index+1<len(argv):return argv[index+1]
                if arg.startswith(name+'='):return arg.partition('=')[2]
        if not option('--memory') or option('--memory-swap')!=option('--memory'):
            raise ValueError('RAM-only workers require an explicit no-swap memory limit')
        if option('--log-driver')!='none':raise ValueError('RAM-only workers require the none log driver')
        paths=[]
        for index,arg in enumerate(argv):
            if arg in ('-v','--volume') or arg.startswith(('--volume=','-v/')):
                raise ValueError('RAM-only workers require explicit --mount declarations')
            if arg=='--mount':
                if index+1==len(argv):raise ValueError('Missing worker mount declaration')
                mount=argv[index+1]
            elif arg.startswith('--mount='):mount=arg.partition('=')[2]
            else:continue
            fields=dict(part.split('=',1) if '=' in part else (part,'') for part in mount.split(','))
            source=fields.get('src',fields.get('source'))
            if fields.get('type','volume')=='bind':
                if not source:raise ValueError('Missing worker bind source')
                target=fields.get('dst',fields.get('target',fields.get('destination')))
                if target in ('/usr','/gpu-runtime') and ('readonly' in fields or 'ro' in fields):continue
                paths.append(source)
            elif fields.get('type','volume')=='volume':
                with self.client() as client:volume=client.volumes.get(source).attrs
                options=volume.get('Options') or {}
                if volume.get('Driver')!='local' or options.get('type')!='tmpfs' or options.get('device')!='tmpfs':
                    raise ValueError('RAM-only workers require tmpfs volumes')
            elif fields.get('type')!='tmpfs':raise ValueError('Unsupported RAM-only worker mount')
        self.validate_worker_storage(paths)

    def dispatch(self, op, args):
        if op=='resource.events':
            if (set(args)!={'after','timeout'} or type(args['after']) is not int or args['after']<0
                    or type(args['timeout']) not in (float,int) or not 0<=args['timeout']<=25):raise ValueError('Invalid resource event cursor')
            with self.resource_condition:
                self.resource_condition.wait_for(lambda:self.resource_revision!=args['after'] or self.storage_stop.is_set(),timeout=args['timeout'])
                return {'revision':self.resource_revision}
        if op=='worker.storage':
            if args:raise ValueError('Storage policy accepts no arguments')
            return self.worker_storage_policy()
        if op=='worker.storage.set':
            if set(args)!={'mode'} or args['mode'] not in ('durable','ram-only'):raise ValueError('Choose durable or ram-only worker storage')
            with self.lock:return self.stream_work.set_storage_policy(args['mode'])
        if op=='worker.storage.validate':
            if set(args)!={'paths'}:raise ValueError('Expected worker mount paths')
            return self.validate_worker_storage(args['paths'])
        if op.startswith('command.'):
            result=self.commands.dispatch(op,args,validate=self.validate_worker_command) if op=='command.start' else self.commands.dispatch(op,args)
            # Idle command pipes must not spin through signed RPC hundreds of times/sec.
            if (op=='command.poll' and not args.get('data') and not result.get('stdout')
                    and not result.get('stderr') and result.get('exit_code') is None):
                time.sleep(.05)
            return result
        if op.startswith('stream.'):
            return self.stream_work.dispatch(op,args)
        if op=='metrics.transport':
            config=json.loads(self.config.read_text())
            if config['name']!=config.get('master','compai.local'):raise ValueError('Telemetry policy requires the master')
            if not isinstance(args,dict) or set(args)!={'transport'} or args['transport'] not in ('tcp','webrtc'):raise ValueError('Choose TCP or WebRTC')
            return self.save_telemetry_policy({'transport':args['transport'],'updated_at':time.time()})
        if op=='resource.gpus':
            if args:raise ValueError('GPU inventory accepts no arguments')
            return self.gpu_resources()
        if op.startswith('resource.cache.'):
            if op!='resource.cache.inventory':
                args=dict(args)
                if args.pop('machine',None)!=hashlib.sha256(Path('/etc/machine-id').read_bytes()).hexdigest():
                    raise ValueError('Cache node machine identity changed')
            if not hasattr(self, 'cache_manager'):
                with self.lock:
                    if not hasattr(self, 'cache_manager'):
                        self.cache_manager = load_cache_runtime().CacheManager(
                            capacity=lambda: ResourceLease.pool_capacity(directory=os.getenv('SWARM_RESOURCE_DIR',str(self.root/'.resources'))),
                            machine=hashlib.sha256(Path('/etc/machine-id').read_bytes()).hexdigest(),
                            accepting=lambda: worker_policy(self.config)['accepting_jobs'])
            if op=='resource.cache.begin':
                # Grow the singleton's commitment atomically with ordinary job
                # admission. A warm keeper cannot borrow its future payload RAM.
                manager=self.cache_manager
                with manager.lock:
                    probe=ResourceLease.__new__(ResourceLease)
                    probe.directory=Path(os.getenv('SWARM_RESOURCE_DIR',str(self.root/'.resources')))
                    probe.directory.mkdir(parents=True,exist_ok=True,mode=0o700)
                    with probe.guard():
                        capacity=manager.capacity
                        manager.capacity=lambda:ResourceLease.pool_capacity(directory=probe.directory,locked=True)
                        try:return manager.dispatch('begin',args)
                        finally:manager.capacity=capacity
            return self.cache_manager.dispatch(op.removeprefix('resource.cache.'), args)
        if op=='resource.caches.inspect':
            if args:raise ValueError('Cache cleanup inventory accepts no arguments')
            return {'items':cache_cleanup_inventory(),'ownership_authority':'controller-submission-queue'}
        if op=='resource.caches.cleanup':
            return cleanup_ram_cache(args)
        if op=='resource.caches':
            if args:raise ValueError('RAM cache inventory accepts no arguments')
            legacy = verified_ram_caches()
            managed = self.dispatch('resource.cache.inventory', {})
            legacy['caches'].extend(i for i in managed['items'] if i['phase']=='ready')
            legacy['pending_caches'].extend(i['key'] for i in managed['items'] if i['phase']!='ready')
            legacy['manager'] = {k:managed[k] for k in ('budget_mb','used_mb')}
            return legacy
        if op=='resource.capacity':
            if set(args)-{'group','include_gpus'} or 'include_gpus' in args and type(args['include_gpus']) is not bool:raise ValueError('Invalid capacity request')
            with self.lock:
                self.reconcile_resources()
                capacity=ResourceLease.pool_capacity(directory=os.getenv('SWARM_RESOURCE_DIR',str(self.root/'.resources')),group=args.get('group'))
                if args.get('include_gpus'):capacity['gpu_inventory']=self.gpu_resources()
                return capacity
        if op in ('resource.acquire','resource.release','resource.renew'):
            with self.lock:
                ident=args.get('id')
                if not isinstance(ident,str) or not re.fullmatch(r'[a-f0-9]{32}',ident):raise ValueError('Invalid job ID')
                if not hasattr(self,'pending_resources'):self.pending_resources={}
                workers=self.reconcile_resources()
                if op=='resource.release':
                    entry=self.pending_resources.pop(ident,None)
                    if entry:entry['lease'].close()
                    self.resource_changed()
                    self.busy();return {'released':True}
                if op=='resource.renew':
                    # Compatibility for existing controllers; no timer or lease owner.
                    if set(args)!={'id'}:raise ValueError('Invalid worker check')
                    if not any(r['job']==ident for r in workers) and ident not in self.pending_resources:
                        raise ValueError('Worker is not active')
                    entry=self.pending_resources.get(ident)
                    if entry:
                        entry['deadline']=time.monotonic()+LAUNCH_RESERVATION_SECONDS
                        entry['lease'].row['handover_until']=time.time()+LAUNCH_RESERVATION_SECONDS;entry['lease'].write()
                    return {'renewed':True,'expires_in_seconds':None,'managed_by':'docker'}
                if set(args)-{'sink','priority'}!={'id','group','cpus','memory_mb','container'} or type(args['cpus']) is not int or args['cpus']<1 or type(args['memory_mb']) is not int or args['memory_mb']<1 or args['container'] not in ('genomics-'+ident,'swarm-job-'+ident):
                    raise ValueError('Expected a job-bound worker name and CPU/RAM limits')
                if type(args.get('priority',0)) is not int or not -1000<=args.get('priority',0)<=1000:raise ValueError('Invalid resource priority')
                sink=job_sink(args)
                existing=next((r for r in workers if r['job']==ident),None)
                if existing:
                    if any(existing[k]!=args[k] for k in ('group','cpus','memory_mb','container')):raise ValueError('Job ID already used')
                    if 'sink' in args and job_sink(existing)!=sink:raise ValueError('Job sink already assigned')
                    return {'acquired':True,'managed_by':'docker'}
                entry=self.pending_resources.get(ident)
                if entry is None:
                    policy=worker_policy(self.config)
                    if not policy['accepting_jobs']:raise RuntimeError(policy['reason'])
                    if len(self.pending_resources)>=128:raise RuntimeError('Worker launch queue full')
                    lease=ResourceLease(args['group'],args['cpus'],args['memory_mb'],args['container'],directory=os.getenv('SWARM_RESOURCE_DIR',str(self.root/'.resources')),pids=32,sink=sink,priority=args.get('priority',0))
                    lease.row.update(job=ident,handover_until=time.time()+LAUNCH_RESERVATION_SECONDS);lease.write()
                    entry={'lease':lease,'deadline':time.monotonic()+LAUNCH_RESERVATION_SECONDS};self.pending_resources[ident]=entry
                lease=entry['lease']
                if any(lease.row[k]!=args[k] for k in ('group','cpus','memory_mb','container')):raise ValueError('Job ID already used')
                if 'sink' in args and job_sink(lease.row)!=sink:raise ValueError('Job sink already assigned')
                entry['deadline']=time.monotonic()+LAUNCH_RESERVATION_SECONDS
                lease.row['handover_until']=time.time()+LAUNCH_RESERVATION_SECONDS;lease.write()
                try:acquired=lease.try_acquire()
                except ValueError:
                    self.pending_resources.pop(ident)['lease'].close();raise
                self.busy();return {'acquired':acquired}
        if op=='worker.policy':return worker_policy(self.config)
        if op in ('job.start','source.stage','p2p.start','image.pull','resource.acquire') or (op=='job.feed' and args.get('tasks')):
            policy=worker_policy(self.config)
            if not policy['accepting_jobs']:raise RuntimeError(policy['reason'])
        if op in ('tests.request','tests.status'):
            config=json.loads(self.config.read_text())
            if not config.get('master_api'):raise RuntimeError('This launcher is not a test master')
            token=(self.config.parent/'master-api-token').read_text().strip()
            if op=='tests.request' and (set(args)-{'paths','suites','force'}):raise ValueError('Invalid test selection')
            request=urllib.request.Request('http://127.0.0.1:8089/api/v1/master/'+('test-requests' if op=='tests.request' else 'status'),
                data=json.dumps(args).encode() if op=='tests.request' else None,
                headers={'Authorization':'Bearer '+token,'Content-Type':'application/json'})
            with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request,timeout=30) as response:
                raw=response.read(8*1024*1024+1)
                if len(raw)>8*1024*1024:raise RuntimeError('Master response too large')
                return json.loads(raw)
        if op=='registry.read':
            from launcher_registry import read
            return read(args,self.config.parent/'layer-cache')
        if op=='image.progress':
            with self.lock:return dict(self.image_progress.get(args['image'], {}))
        if op=='image.pull':
            from launcher_registry import pull
            if Path('/tmp/launcher-agent-draining').exists():raise RuntimeError('Agent is draining for an explicit update')
            with self.lock:self.registry_transfers+=1;self.busy()
            try:return pull(self,args)
            finally:
                with self.lock:self.registry_transfers-=1;self.busy()
        if op == 'worker.status':return self.worker_status(args)
        if op == 'worker.records':
            if set(args)-{'after'} or not isinstance(args.get('after',''),str):raise ValueError('Invalid worker journal cursor')
            rows=self.worker_journal.records(args.get('after',''))
            return {'items':rows,'next':rows[-1]['job'] if len(rows)==100 else None}
        if op == 'metrics':
            return self.telemetry.snapshot(json.loads(self.config.read_text())['name'])
        if op == 'metrics.fleet':
            config=json.loads(self.config.read_text())
            if config['name']!=config.get('master','compai.local'):raise ValueError('Telemetry receiver is not the master')
            after=args.get('after',0) if args.get('session')==self.telemetry.session else 0
            if type(after) is not int or after<0:raise ValueError('Invalid telemetry cursor')
            return {**self.telemetry.fleet(config['peers'],after),'policy':self.telemetry_policy()}
        if op == 'gpu.probe':
            if args:raise ValueError('GPU probe does not accept commands or image overrides')
            if not self.gpu_lock.acquire(blocking=False):raise RuntimeError('GPU validation already running')
            lease=None
            try:
                name='orgportal-test-gpu-'+secrets.token_hex(8)
                lease=ResourceLease('gpu-probe',1,512,name,directory=os.getenv('SWARM_RESOURCE_DIR',str(self.root/'.resources')),pids=64)
                if not lease.try_acquire():return {'waiting_capacity':True}
                import docker
                with self.client() as client:
                    if 'nvidia' not in client.info().get('Runtimes',{}):raise RuntimeError('NVIDIA container runtime is not configured')
                    # Existing image only: readiness checks never pull from WAN.
                    image=client.images.get('python:3.12-bookworm')
                    container=client.containers.run(image.id,['python','-c',GPU_SMOKE],detach=True,name=name,
                        network_mode='none',read_only=True,cap_drop=['ALL'],security_opt=['no-new-privileges'],
                        mem_limit='512m',pids_limit=64,nano_cpus=1000000000,
                        device_requests=[docker.types.DeviceRequest(count=-1,capabilities=[['gpu']])])
                    try:
                        status=container.wait(timeout=40)
                        logs=container.logs().decode(errors='replace')[:4000]
                        if status['StatusCode']:raise RuntimeError('CUDA kernel probe failed: '+logs[-800:])
                        result=json.loads(logs)
                    finally:container.remove(force=True)
                self.gpu_validation={**result,'verified_at':time.time()}
                return self.gpu_validation
            finally:
                if lease:lease.close()
                self.gpu_lock.release()
        if op == 'status':
            policy=worker_policy(self.config)
            config = json.loads(self.config.read_text())
            import socket
            with self.client() as client:
                info = client.info()
            machine = Path('/etc/machine-id')
            if not machine.exists():raise RuntimeError('Host machine identity is not mounted')
            rows = json.loads(subprocess.check_output(['ip','-j','addr'],text=True,timeout=5))
            addresses = [a['local'].split('%')[0] for row in rows if row['ifname'].startswith(('en','eth','wl')) for a in row.get('addr_info',[]) if a.get('scope')!='host']
            try:update_state=json.loads(Path('/tmp/launcher-agent-update.json').read_text())
            except (OSError,ValueError):update_state={}
            return {'service':'swarm-node','update':update_state,'lifecycle':'updating' if policy['mode']=='draining' else 'ready' if policy['accepting_jobs'] else 'paused','protocol':VERSION,'source_revision':self.source_revision,'node':config['name'],'ready':bool(shutil.which('docker')) and not Path('/tmp/launcher-agent-draining').exists(),'time':time.time(),
                    'machine':hashlib.sha256(machine.read_bytes()).hexdigest(),'hostname':socket.gethostname(),
                    'addresses':addresses,'platform':info['OSType']+'/'+info['Architecture'],
                    'routing':getattr(self,'routing_status',{'status':'pending'}),
                    'worker_policy':policy,'capabilities':['controller-signature','github-jobs','p2p-browser','cancel','events','layer-pull','registry-read','peer-layer-cache','mptcp-transport','file-queue','snapshot-stage','event-wait','shared-resources','renewable-resources','docker-workers','durable-docker-workers-v1','execution-accounting-v1','persistent-command-pipes-v1','generic-workers','encrypted-backup','gpu-kernel','telemetry-push','performance-v1','builtin-monitor','stream-work-v1','independent-job-sinks-v1','node-command-v1','gpu-resource-inventory-v1','resource-events-v1','node-maintenance-recipes-v1','verified-ram-caches-v1','cache-cleanup-v1','node-cache-v1','worker-storage-v1','node-web-v1'],
                    'cache_summary':getattr(getattr(self,'cache_manager',None),'summary',{}),'enrolled_peers':sorted(config['peers']),'storage':self.storage_status,'storage_policy':self.worker_storage_policy(),
                    'hardware':self.hardware(info),
                    'preparations':[key for key,j in self.jobs.items() if j['status']=='running' and j.get('prepare_only')],
                    'active_commands':self.commands.active() if hasattr(self,'commands') else 0,'active_jobs':[key for key,j in self.jobs.items() if j['status']=='running' and not j.get('prepare_only')],'active_image_transfers':self.registry_transfers,'active_uploads':len(self.uploads),'active_resource_leases':len(self.pending_resources),'workers':self.reconcile_resources()}
        if op.startswith('backup.'):
            return self.backup(op,args)
        if op.startswith('upload.'):
            return self.upload(op,args)
        if op == 'image.inspect':
            import docker
            with self.client() as client:
                try:
                    image = client.images.get(args['image'])
                    return {'id':image.id,'os':image.attrs['Os'],'architecture':image.attrs['Architecture'],'fingerprint':self.image_fingerprint(image)}
                except docker.errors.ImageNotFound:return None
        if op == 'source.stage':
            args={**args,'manifest':{**args['manifest'],'prepare_only':True}}
            manifest=args['manifest']
            with self.lock:
                for ident,job in self.jobs.items():
                    if (job.get('prepare_only') and job.get('workspace_retained',True) and job['status'] in ('running','completed')
                            and not (self.folder(ident)/'STOP').exists()
                            and job.get('source_sha256')==manifest.get('source_sha256')
                            and job.get('images')==manifest['hosts'][args['index']].get('images')):
                        return {'id':ident,'status':job['status']}
            return self.start(args)
        if op == 'job.start':return self.start(args)
        if op == 'job.feed':
            folder=self.folder(args['id'])
            with self.lock:
                job=self.jobs.get(args['id'])
                # A lost terminal acknowledgement can arrive after the worker exits.
                # Acknowledge only the already-persisted empty close; never add work.
                if (job and job['status']=='completed' and args.get('close') is True and
                        args.get('tasks',[])==[] and not (folder/'STOP').exists() and
                        (folder/'tasks/CLOSED').is_file()):
                    return {'accepted':[],'closed':True}
                if not job or job['status']!='running':raise ValueError('Worker session is not running')
                if (folder/'STOP').exists():raise ValueError('Worker session is stopping')
                manifest=json.loads((folder/'manifest.json').read_text())
                if not manifest.get('granular'):raise ValueError('Worker is not a task-queue session')
                tasks=args.get('tasks',[])
                if not isinstance(tasks,list) or len(tasks)>128 or any(not isinstance(t,str) or t not in manifest['task_catalog'] for t in tasks):
                    raise ValueError('Tasks must belong to the pinned run catalog')
                inbox=folder/'tasks';inbox.mkdir(exist_ok=True,mode=0o700)
                if (inbox/'CLOSED').exists() and tasks:raise ValueError('Worker queue is closed')
                for task in tasks:
                    # Identifiers are catalog hashes, never shell commands or paths.
                    if not re.fullmatch('[a-z0-9-]{1,80}',task):raise ValueError('Invalid task identifier')
                    (inbox/task).touch(exist_ok=True)
                if args.get('p2p_finished') is True:(folder/'P2P_FINISHED').touch()
                if args.get('close'):(inbox/'CLOSED').touch()
                return {'accepted':tasks,'closed':bool(args.get('close'))}
        if op == 'job.poll':
            ident=args['id'];folder=self.folder(ident)
            with self.lock:
                job=self.jobs.get(ident)
                if job is None:
                    events=persisted_job_events(self.root,ident,int(args.get('cursor',0)))
                    state=json.loads((folder/'state.json').read_text()) if (folder/'state.json').exists() else {'status':'interrupted'}
                    if state.get('status') in ('running','starting'):state['status']='interrupted'
                    return {**state,'events':events or [],'cursor':events[-1]['sequence'] if events else int(args.get('cursor',0)), 'events_lost':events is None}
                cursor=int(args.get('cursor',0))
                wait=min(20,max(0,float(args.get('wait_seconds',0))))
                self.changed.wait_for(lambda:job['sequence']>cursor or job['status']!='running',timeout=wait)
                persisted=persisted_job_events(self.root,ident,cursor)
                events=persisted if persisted is not None else [{'sequence':n,'event':event} for n,event in job['events'] if n>cursor][:200]
                return {'status':job['status'],'exit_code':job.get('exit_code'),'error':job.get('error'),'preparation':job.get('preparation'),
                        'events':events,'cursor':events[-1]['sequence'] if events else cursor,
                        'events_lost':persisted is None and bool(job['events'] and cursor<job['events'][0][0]-1)}
        if op == 'job.stop':
            folder=self.folder(args['id']);folder.mkdir(exist_ok=True,mode=0o700)
            (folder/'STOP').touch()
            with self.changed:self.changed.notify_all()
            self.remove_containers(args['id'])
            return {'acknowledged':True}
        if op == 'p2p.start':return self.p2p_start(args)
        if op == 'p2p.webdriver':
            folder=self.folder(args['id']+'-p2p')
            if not (folder/'browser.json').exists():raise ValueError('No owned P2P browser')
            path=args['path']
            if not isinstance(path,str) or not re.fullmatch(r'/(?:status|session(?:/[A-Za-z0-9_/-]+)?)',path):raise ValueError('Invalid WebDriver path')
            method=args.get('method','POST')
            if method not in ('GET','POST','DELETE'):raise ValueError('Invalid WebDriver method')
            record=json.loads((folder/'browser.json').read_text())
            with self.client() as client:
                return browser_request(client.containers.get(record['container']),path,args.get('data'),method)
        if op == 'p2p.stop':
            ident=args['id']+'-p2p';folder=self.folder(ident)
            self.remove_containers(args['id'])
            shutil.rmtree(folder,ignore_errors=True)
            with self.lock:
                timer=self.browser_timers.pop(args['id'],None)
                if timer:timer.cancel()
                self.browsers.discard(args['id']);self.busy()
                lease=getattr(self,'p2p_leases',{}).pop(args['id'],None)
                if lease:lease.close()
            return {'acknowledged':True}
        raise ValueError('Unsupported agent operation')

    def backup(self,op,args):
        """Owner-authenticated encrypted replicas; fixed private storage, no extraction."""
        root=Path(self.config).parent/'backups';root.mkdir(exist_ok=True,mode=0o700)
        def checked(value):
            if not isinstance(value,dict) or value.get('version')!=1 or not re.fullmatch(r'[0-9]{8}T[0-9]{6}Z',str(value.get('snapshot',''))):raise ValueError('Invalid backup identity')
            if value.get('encryption')!='AES-256-GCM/RSA-OAEP-SHA256' or not all(re.fullmatch('[a-f0-9]{64}',str(value.get(k,''))) for k in ('sourceManifestSha256','recipientSha256')):raise ValueError('Backup must be encrypted')
            if not 256<=len(base64.b64decode(value.get('wrappedKey',''),validate=True))<=1024:raise ValueError('Invalid recipient key')
            parts=value.get('parts')
            if not isinstance(parts,list) or not 1<=len(parts)<=8192:raise ValueError('Invalid backup parts')
            if any(not isinstance(p,dict) or not re.fullmatch('[a-f0-9]{64}',str(p.get('sha256',''))) or type(p.get('size')) is not int or not 28<p['size']<=64*1024**2 for p in parts):raise ValueError('Invalid backup part')
            if len({p['sha256'] for p in parts})!=len(parts):raise ValueError('Duplicate backup parts')
            return value
        def save(path,value):
            temporary=path.with_suffix('.tmp')
            with open(temporary,'w',opener=lambda p,flags:os.open(p,flags,0o600)) as stream:
                json.dump(value,stream);stream.flush();os.fsync(stream.fileno())
            temporary.replace(path)
        def good(path,part):
            if not path.is_file() or path.stat().st_size!=part['size']:return False
            with path.open('rb') as stream:return sha256_stream(stream)==part['sha256']
        with self.lock:
            if op=='backup.status':
                copies=[]
                for path in sorted(root.glob('*/manifest.json'),reverse=True)[:50]:
                    value=json.loads(path.read_text());copies.append({'snapshot':value['snapshot'],'parts':len(value['parts']),'bytes':sum(p['size'] for p in value['parts']),'path':str(path.parent)})
                return {'root':str(root),'freeBytes':shutil.disk_usage(root).free,'copies':copies}
            if op=='backup.begin':
                value=checked(args['manifest']);folder=root/value['snapshot'];folder.mkdir(exist_ok=True,mode=0o700)
                for name in ('manifest.json','pending.json'):
                    existing=folder/name
                    if existing.exists() and json.loads(existing.read_text())!=value:raise ValueError('Backup generation is immutable')
                missing=[p for p in value['parts'] if not good(folder/(p['sha256']+'.enc'),p)]
                if shutil.disk_usage(root).free<sum(p['size'] for p in missing)+512*1024**2:raise RuntimeError('Insufficient backup disk space')
                save(folder/'pending.json',value)
                return {'missing':[p['sha256'] for p in missing],'path':str(folder),'freeBytes':shutil.disk_usage(root).free}
            ident=args.get('snapshot','')
            if not isinstance(ident,str) or not re.fullmatch(r'[0-9]{8}T[0-9]{6}Z',ident):raise ValueError('Invalid backup snapshot')
            folder=root/ident
            pending=folder/'pending.json';value=checked(json.loads((pending if pending.exists() else folder/'manifest.json').read_text()))
            if op=='backup.put':
                digest=args.get('sha256');part=next((p for p in value['parts'] if p['sha256']==digest),None)
                if not part:raise ValueError('Part is not in this backup')
                source=self.completed_upload(args['upload'])
                if not good(source,part):raise ValueError('Backup upload checksum mismatch')
                target=folder/(digest+'.enc')
                if not good(target,part):
                    temporary=folder/(digest+'.pending');shutil.copyfile(source,temporary);temporary.chmod(0o600)
                    with temporary.open('rb') as stream:os.fsync(stream.fileno())
                    temporary.replace(target)
                self.release_upload(args['upload'])
                return {'stored':True,'sha256':digest,'bytes':part['size']}
            if op=='backup.commit':
                if any(not good(folder/(p['sha256']+'.enc'),p) for p in value['parts']):raise RuntimeError('Backup is incomplete or corrupt')
                save(folder/'manifest.json',value);pending.unlink(missing_ok=True)
                descriptor=os.open(folder,os.O_RDONLY)
                try:os.fsync(descriptor)
                finally:os.close(descriptor)
                return {'verified':True,'snapshot':ident,'parts':len(value['parts']),'bytes':sum(p['size'] for p in value['parts']),'path':str(folder)}
            raise ValueError('Unsupported backup operation')

    def upload(self, op, args):
        with self.lock:
            # Expired partial transfers are reclaimed; enforce a bounded staging quota.
            for token,row in list(self.uploads.items()):
                if time.time()-row['updated']>3600:
                    row['path'].unlink(missing_ok=True);del self.uploads[token]
            if op == 'upload.begin':
                size=args['size'];digest=args['sha256']
                if not isinstance(size,int) or not 0<size<=8*1024**3 or not re.fullmatch('[a-f0-9]{64}',digest):raise ValueError('Invalid upload bounds or hash')
                if len(self.uploads)>=4 or sum(row['size'] for row in self.uploads.values())+size>12*1024**3:raise RuntimeError('Agent upload quota reached')
                if shutil.disk_usage(self.root).free<size+256*1024**2:raise RuntimeError('Insufficient agent staging space')
                token=secrets.token_hex(16);path=self.root/(token+'.upload');path.touch(mode=0o600)
                self.uploads[token]={'path':path,'size':size,'sha256':digest,'offset':0,'updated':time.time(),'complete':False}
                self.busy()
                return {'upload':token}
            row=self.uploads[args['upload']]
            if op == 'upload.chunk':
                data=base64.b64decode(args['data'],validate=True)
                offset=args['offset']
                if len(data)>LIMIT or offset!=row['offset'] or offset+len(data)>row['size'] or row['complete']:raise ValueError('Invalid upload chunk or offset')
                with row['path'].open('ab') as stream:stream.write(data)
                row.update(offset=offset+len(data),updated=time.time())
                return {'offset':row['offset']}
            if op == 'upload.finish':
                with row['path'].open('rb') as stream:digest=sha256_stream(stream)
                if row['offset']!=row['size'] or digest!=row['sha256']:raise ValueError('Upload checksum mismatch')
                row['complete']=True
                return {'verified':True}
            raise ValueError('Unsupported upload operation')

    def release_upload(self, token):
        with self.lock:
            row=self.uploads.pop(token)
            row['path'].unlink(missing_ok=True)
            self.busy()

    def completed_upload(self, token):
        with self.lock:
            row=self.uploads[token]
            if not row['complete']:raise ValueError('Upload not verified')
            return row['path']

    def extract(self, token, work):
        path=self.completed_upload(token);total=0;count=0
        work.mkdir(parents=True,mode=0o755)
        with tarfile.open(path) as archive:
            for member in archive:
                name=Path(member.name);total+=member.size;count+=1
                if not member.isfile() or name.is_absolute() or '..' in name.parts or total>2*1024**3 or count>100000:raise ValueError('Unsafe or oversized snapshot')
                target=work/name
                if target.exists():raise ValueError('Duplicate snapshot entry')
                target.parent.mkdir(parents=True,exist_ok=True,mode=0o755)
                with archive.extractfile(member) as source,target.open('wb') as destination:shutil.copyfileobj(source,destination)
                target.chmod(0o755 if member.mode&0o111 else 0o644)
        self.release_upload(token)

    def start(self, args):
        if Path('/tmp/launcher-agent-draining').exists():raise RuntimeError('Agent is draining for an explicit update')
        ident=args['id'];folder=self.folder(ident)
        self.cleanup_workspaces()
        with self.lock:
            if ident in self.jobs:return {'status':self.jobs[ident]['status'],'id':ident}
            if shutil.disk_usage(self.root).free<MIN_FREE_BYTES:raise RuntimeError('Worker needs at least 10 GiB free disk space after cleanup')
            if folder.exists():raise ValueError('Run ID already exists; use a new run')
            preparing=args['manifest'].get('prepare_only') is True
            count=sum(j['status']=='running' and bool(j.get('prepare_only'))==preparing for j in self.jobs.values())
            if count >= (1 if preparing else 8):raise RuntimeError('Worker session limit reached')
            work=folder/'source'
            manifest=args['manifest']
            repositories=manifest.get('repositories',{})
            if not {'.','OrgPortal','PIdP','CodeCollective'}.issubset(repositories) or len(repositories)>24:raise ValueError('Expected the application GitHub repositories and bounded submodules')
            for name in repositories:
                if name not in ('.','JSwarm','GenomeHarbor','OrgPortal','PIdP','CodeCollective','BmoreMedTech') and (Path(name).is_absolute() or '..' in Path(name).parts or not name.startswith(('JSwarm/','GenomeHarbor/','OrgPortal/','PIdP/','CodeCollective/','BmoreMedTech/'))):raise ValueError('Invalid nested repository path')
            for repo in repositories.values():
                if not re.fullmatch(r'https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\.git',repo.get('url','')) or not re.fullmatch('[a-f0-9]{40}',repo.get('commit','')):raise ValueError('Invalid GitHub repository or commit')
            refs={name:{key:repo[key] for key in ('url','commit')} for name,repo in repositories.items()}
            digest=hashlib.sha256(json.dumps(refs,sort_keys=True,separators=(',',':')).encode()).hexdigest()
            if manifest.get('source_sha256')!=digest:raise ValueError('GitHub source manifest checksum mismatch')
            index=args['index']
            if not isinstance(index,int) or not 0<=index<len(manifest['hosts']):raise ValueError('Invalid shard index')
            target=manifest['hosts'][index]
            config=json.loads(self.config.read_text())
            if manifest['id']!=ident or target['host']!=config['name']:raise ValueError('GitHub manifest targets a different node or run')
            if not preparing:
                # Explicit test work takes priority over speculative installers.
                for key,previous in self.jobs.items():
                    if previous['status']=='running' and previous.get('prepare_only'):
                        (self.folder(key)/'STOP').touch()
                self.changed.notify_all()
            folder.mkdir(mode=0o700)
            (folder/'manifest.json').write_text(json.dumps(manifest))
            job={'status':'running','prepare_only':preparing,'events':collections.deque(maxlen=10000),'sequence':0}
            job.update(source_sha256=digest,images=target.get('images',{}))
            self.jobs[ident]=job
            with self.changed:self.changed.notify_all()
            self.busy()
            (folder/'state.json').write_text(json.dumps({'status':'running'}))
            self.active_workers.add(ident)
            threading.Thread(target=self.worker,args=(ident,work,index,job),daemon=True).start()
            return {'id':ident,'status':'running'}

    def checkout(self,work,manifest,env,job):
        def git(path,*args,local=False,required=True):
            if (work.parent/'STOP').exists():raise InterruptedError('GitHub synchronization stopped')
            result=subprocess.run(['git','-c','core.hooksPath=/dev/null',
                                   '-c','protocol.file.allow='+('always' if local else 'never'),
                                   '-c','submodule.recurse=false','-C',str(path),*args],
                                  capture_output=True,text=True,env=env,timeout=180)
            if result.returncode:
                if not required:return None
                raise RuntimeError('Source Git operation failed: '+args[0]+'; check access and the pinned commit')
            return result.stdout.strip()

        if not manifest.get('prepare_only'):
            with self.storage_lock,self.lock:
                for key,previous in self.jobs.items():
                    if previous.get('prepare_only') and previous.get('status')=='completed':
                        source=self.folder(key)/'source'
                        marker=source/'.prepared-packages.json'
                        if not marker.exists():continue
                        prepared=json.loads(marker.read_text())
                        target=manifest['hosts'][next(i for i,h in enumerate(manifest['hosts']) if h['host']==json.loads(self.config.read_text())['name'])]
                        if prepared.get('source_sha256')!=manifest['source_sha256'] or any(prepared.get('images',{}).get(k)!=v for k,v in target['images'].items()):continue
                        if not all((source/name/'.git').exists() for name in ('.','OrgPortal','PIdP','CodeCollective')):continue
                        source.rename(work)
                        (work/'distributed-plan.json').write_text(json.dumps(manifest))
                        previous['status']='consumed'
                        persist_job_event(self.root,manifest['id'],job,{'kind':'source','repository':'.','commit':manifest['repositories']['.']['commit'],'status':'prepared_snapshot','cache_hit':True})
                        self.changed.notify_all()
                        return

        target=next(host for host in manifest['hosts'] if host['host']==json.loads(self.config.read_text())['name'])
        # The runner always comes from Organization; application repositories are
        # selected by the actual shard, not by the whole fleet's manifest.
        needed=required_test_repositories(target['suites'],target.get('jobs'),manifest['repositories'])
        cache_root=self.root/'git-cache'
        cache_root.mkdir(exist_ok=True,mode=0o700)

        def event(name,repo,status,**details):
            with self.lock:
                persist_job_event(self.root,manifest['id'],job,{'kind':'source','repository':name,
                    'commit':repo['commit'],'status':status,**details})
                self.changed.notify_all()

        for name in sorted(needed,key=lambda value:(len(Path(value).parts),value)):
            repo=manifest['repositories'][name];path=work/name
            if path.is_symlink():raise ValueError('Repository path must not be a symlink')
            path.mkdir(parents=True,exist_ok=True,mode=0o755)
            if any(path.iterdir()):raise ValueError('Checkout directory must be fresh: '+name)
            sparse = name == 'CodeCollective'
            identity=hashlib.sha256(repo['url'].encode()).hexdigest()
            cache=cache_root/(identity+'.git')
            if cache.is_symlink():raise ValueError('Git cache must not be a symlink')
            # One cache per repository URL, retained independently of run cleanup.
            # No network operation occurs when the exact pinned commit is present.
            with (cache_root/(identity+'.lock')).open('a') as lock:
                fcntl.flock(lock,fcntl.LOCK_EX)
                # Every writer owns this lock. A leftover Git lock belongs to an
                # interrupted fetch; discard only this disposable source cache.
                if (cache/'shallow.lock').exists():shutil.rmtree(cache)
                if not cache.exists():
                    cache.mkdir(mode=0o700)
                    git(cache,'init','--bare','--quiet')
                # A stopped installer may leave an initialized cache before
                # remote creation. Repair it while holding the writer lock.
                if git(cache,'remote','get-url','origin',required=False) is None:
                    git(cache,'remote','add','origin',repo['url'])
                if git(cache,'remote','get-url','origin')!=repo['url']:
                    raise ValueError('Git cache origin differs from the source manifest')
                if sparse:
                    git(cache,'config','remote.origin.promisor','true')
                    git(cache,'config','remote.origin.partialclonefilter','blob:none')
                    git(cache,'config','uploadpack.allowFilter','true')
                cached=git(cache,'cat-file','-e',repo['commit']+'^{commit}',required=False) is not None
                seeded=False
                if not cached:
                    for previous in sorted(self.root.glob('*/source'),reverse=True)[:30]:
                        source=previous/name
                        if source==path or source.is_symlink() or not (source/'.git').is_dir():continue
                        if git(source,'remote','get-url','origin',required=False)!=repo['url']:continue
                        if git(source,'cat-file','-e',repo['commit']+'^{commit}',required=False) is None:continue
                        git(cache,'fetch','--no-tags','--depth=1',str(source),
                            repo['commit']+':refs/cache/'+repo['commit'],local=True)
                        cached=True;seeded=True;break
                event(name,repo,'cached' if cached else 'fetching',cache_hit=cached,seeded_from_previous_run=seeded)
                if not cached:
                    git(cache,'fetch','--no-tags','--depth=1',*(['--filter=blob:none'] if sparse else []),'origin',
                        repo['commit']+':refs/cache/'+repo['commit'])
                if git(cache,'rev-parse',repo['commit']+'^{commit}')!=repo['commit']:
                    raise ValueError('Git cache returned a different commit')
                # Local cloning hardlinks immutable objects where supported. Each
                # run owns its refs/index/worktree and has no external alternates.
                git(work,'clone','--local','--no-checkout',str(cache),str(path),local=True)
                # Cache commits live under refs/cache, which clone does not advertise.
                # Fetch the exact object locally so a shallow cache can materialize it.
                if sparse:
                    # Git can disable its local hardlink optimization for partial
                    # repositories. Retain already-cached immutable objects too.
                    for source_object in (cache/'objects').rglob('*'):
                        if not source_object.is_file():continue
                        destination=path/'.git/objects'/source_object.relative_to(cache/'objects')
                        if destination.exists():continue
                        destination.parent.mkdir(parents=True,exist_ok=True)
                        os.link(source_object,destination)
                    git(path,'config','remote.origin.promisor','true')
                    git(path,'config','remote.origin.partialclonefilter','blob:none')
                git(path,'fetch','--no-tags','--depth=1',*(['--filter=blob:none'] if sparse else []),'origin' if sparse else str(cache),repo['commit'],local=True)
            git(path,'remote','set-url','origin',repo['url'])
            if sparse:
                git(path,'sparse-checkout','set','--no-cone','/run.py','/usa/','/css/','/js/reusableComponents.js','/tests/usajobs-mobile.test.cjs')
            # Only missing sparse blobs are fetched from the verified GitHub origin.
            git(path,'checkout','--detach','--force',repo['commit'])
            if git(path,'rev-parse','HEAD')!=repo['commit'] or git(path,'status','--porcelain'):
                raise ValueError('Checkout verification failed: '+name)
            event(name,repo,'verified',cache_hit=cached)
        (work/'distributed-plan.json').write_text(json.dumps(manifest))

    def worker(self, ident, work, index, job):
        folder=work.parent
        try:
            home=self.root/'home';home.mkdir(exist_ok=True,mode=0o700)
            env=worker_environment(home,work)
            env['SWARM_RESOURCE_DIR']=str(Path(os.getenv('SWARM_RESOURCE_DIR',str(self.root/'.resources'))))
            manifest=json.loads((folder/'manifest.json').read_text())
            self.checkout(work,manifest,env,job)
            if (folder/'STOP').exists():raise InterruptedError('Run stopped during GitHub synchronization')
            if manifest.get('prepare_only'):
                # Older pinned runners may not implement preparation-only mode.
                # Cache their Git objects, but never accidentally execute tests.
                if manifest.get('sources_only') or 'PREPARATION_PROTOCOL = 1' not in (work/'swarm-control/server/distributed_worker.py').read_text():
                    job.update(status='completed',preparation='sources_only');return
                while True:
                    if (folder/'STOP').exists():raise InterruptedError('Preparation superseded')
                    with self.lock:
                        busy=any(j is not job and j['status']=='running' and not j.get('prepare_only') for j in self.jobs.values()) or bool(self.browsers)
                    if not busy:break
                    time.sleep(.5)
            process=subprocess.Popen([sys.executable,str(work/'swarm-control/server/distributed_worker.py'),str(index)],
                stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,errors='replace',env=env,start_new_session=True)
            def expire():
                (folder/'STOP').touch()
                self.remove_containers(ident)
                if process.poll() is None:os.killpg(process.pid,signal.SIGTERM)
            timer=threading.Timer(7200,expire);timer.start()
            try:
                with contextlib.closing(JobEventWriter(self.root)) as journal:
                    for line in process.stdout:
                        if len(line)>16000:continue
                        try:event=json.loads(line)
                        except ValueError:event={'kind':'log','line':line[:2000]}
                        # Disk I/O must never hold the agent's global status lock.
                        # This worker is the only sequence writer for this job.
                        sequence=persist_job_event(self.root,ident,job,event,writer=journal,publish=False)
                        with self.lock:
                            if event.get('kind')=='prepared':job['preparation']='ready'
                            job['sequence']=sequence;job['events'].append((sequence,event))
                            self.changed.notify_all()
                    code=process.wait(timeout=30)
            finally:timer.cancel()
            job.update(status='stopped' if (folder/'STOP').exists() else 'completed' if code==0 else 'failed',exit_code=code)
        except InterruptedError as error:job.update(status='stopped',error=str(error)[:800])
        except Exception as error:job.update(status='failed',error=type(error).__name__+': '+str(error)[:800])
        finally:
            try:self.remove_containers(ident)
            except Exception as error:job.update(status='failed',error='Test container cleanup failed: '+str(error)[:400])
            with self.changed:self.changed.notify_all()
            self.busy()
            job['finished_at']=time.time()
            (folder/'state.json').write_text(json.dumps({key:value for key,value in job.items() if key!='events'}))
            with self.lock:self.active_workers.discard(ident)
            self.cleanup_workspaces()

    def p2p_start(self,args):
        ident=args['id'];self.folder(ident) # Validate before admitting a reservation.
        with self.lock:
            leases=getattr(self,'p2p_leases',None)
            if leases is None:self.p2p_leases={};leases=self.p2p_leases
            if ident not in leases:
                leases[ident]=ResourceLease(ident,2,2048,'orgportal-test-'+ident+'-p2p',
                    directory=os.getenv('SWARM_RESOURCE_DIR',str(self.root/'.resources')))
            lease=leases[ident]
            if not lease.try_acquire():return {'waiting_capacity':True}
        try:return self.start_p2p_browser(args)
        except Exception:
            with self.lock:leases.pop(ident,None);lease.close()
            raise

    def start_p2p_browser(self,args):
        if Path('/tmp/launcher-agent-draining').exists():raise RuntimeError('Agent is draining for an explicit update')
        ident=args['id'];folder=self.folder(ident+'-p2p')
        if (self.folder(ident)/'STOP').exists():raise RuntimeError('Run was stopped')
        if folder.exists():raise ValueError('Browser already prepared for this run')
        if self.browsers:raise RuntimeError('An agent P2P browser is already active')
        self.extract(args['upload'],folder)
        if set(p.name for p in folder.iterdir())!={'remote.py','peer.js'}:raise ValueError('Unexpected P2P fixture files')
        # A native agent may use umask 077; these public fixtures must be
        # readable by Selenium's distinct container UID, including rootless UID maps.
        folder.chmod(0o755)
        with self.client() as client:
            image=client.images.get(args['image'])
            if image.id!=args['image']:raise ValueError('P2P requires immutable image ID')
            container=client.containers.run(image.id, ['/p2p/remote.py'],entrypoint='/usr/bin/python3',
                detach=True,name='orgportal-test-'+ident+'-p2p',network_mode='host',nano_cpus=1500000000,
                mem_limit='2g',pids_limit=512,user='1200:1201',shm_size='256m',cap_drop=['ALL'],
                volumes={str(folder):{'bind':'/p2p','mode':'ro'}},environment={
                    'SE_OPTS':'--host 127.0.0.1 --port 4449','SE_START_VNC':'false','SE_START_NO_VNC':'false',
                    'SE_START_XVFB':'false','SE_NODE_MAX_SESSIONS':'1'})
        if (self.folder(ident)/'STOP').exists():
            self.remove_containers(ident);raise RuntimeError('Run stopped during browser startup')
        with self.lock:self.browsers.add(ident);self.busy()
        (folder/'browser.json').write_text(json.dumps({'container':container.id}))
        timer=threading.Timer(1800,lambda:self.dispatch('p2p.stop',{'id':ident}))
        timer.daemon=True;self.browser_timers[ident]=timer;timer.start()
        return {'started':True}

    def run_gpu_probe(self,args,*,timeout,prefix):
        """Serialize probes and settle failed removals before creating more helpers."""
        with self.__dict__.setdefault('gpu_probe_lock',threading.Lock()):
            def remove(name):
                result=subprocess.run(['docker','rm','-f',name],capture_output=True,text=True,timeout=30)
                if result.returncode and 'No such container' not in result.stderr:
                    raise RuntimeError('GPU helper cleanup failed: '+result.stderr[-300:])
            pending=getattr(self,'gpu_probe_pending',None)
            if pending:
                remove(pending)
                self.gpu_probe_pending=None
            name=prefix+secrets.token_hex(8)
            self.gpu_probe_pending=name
            try:
                result=subprocess.run(['docker','run','--name',name,*args],capture_output=True,text=True,check=True,timeout=timeout)
            except BaseException:
                # Retain ownership if Docker is still unavailable. The next
                # probe retries removal instead of adding another container.
                remove(name)
                self.gpu_probe_pending=None
                raise
            self.gpu_probe_pending=None  # --rm completed successfully.
            return result

    def host_render_groups(self):
        if getattr(self,'gpu_render_groups',None) is not None and time.time()-self.gpu_render_groups_at<30:return self.gpu_render_groups
        # Only metadata: no character-device permission is granted to this helper.
        code="""import pathlib,json,platform
result={'groups':{p.name:p.stat().st_gid for p in pathlib.Path('/host-dri').glob('renderD*')},'icds':[]}
for p in pathlib.Path('/host-share/vulkan/icd.d').glob('*.json'):
 try:
  library=json.loads(p.read_text()).get('ICD',{}).get('library_path')
  if not isinstance(library,str):continue
  if platform.machine()=='x86_64' and any(x in (str(p)+library).lower() for x in ('i386','i686')):continue
  result['icds'].append({'path':'/usr/share/vulkan/icd.d/'+p.name,'library':library})
 except (OSError,ValueError,TypeError):continue
print(json.dumps(result))"""
        try:
            result=self.run_gpu_probe(['--rm','--pull=never','--network','none','--read-only','--cap-drop','ALL','--security-opt','no-new-privileges','--memory','64m','--memory-swap','64m','--cpus','0.1','--pids-limit','16','--label','org.lifetech.service=gpu-inventory','--mount','type=bind,src=/dev/dri,dst=/host-dri,readonly','--mount','type=bind,src=/usr/share,dst=/host-share,readonly','organization-launcher:webrtc-1','python3','-c',code],timeout=10,prefix='swarm-gpu-inventory-render-')
            metadata=json.loads(result.stdout)
            groups=metadata.get('groups',metadata)
            if 'icds' in metadata:
                icds=metadata['icds']
                if not isinstance(icds,list) or any(not isinstance(x,dict) or not isinstance(x.get('path'),str) or not isinstance(x.get('library'),str) for x in icds):raise ValueError('Invalid host Vulkan manifests')
                self.gpu_host_icds=icds
            if not isinstance(groups,dict) or any(not re.fullmatch(r'renderD[0-9]+',k) or type(v) is not int or v<0 for k,v in groups.items()):raise ValueError('Invalid host device groups')
        except (OSError,ValueError,RuntimeError,subprocess.SubprocessError):return {}
        self.gpu_render_groups=groups;self.gpu_render_groups_at=time.time();return groups

    def host_intel_devices(self,render,gid,icd):
        """Classify physical Intel Vulkan devices; heap budgets are not free VRAM."""
        key=(render,gid,icd);cache=self.__dict__.setdefault('intel_device_cache',{})
        if key in cache and time.time()-cache[key][0]<60:return cache[key][1]
        code=r"""import ctypes as c,json
v=c.CDLL('libvulkan.so.1')
class Info(c.Structure):
 _fields_=[('type',c.c_uint32),('next',c.c_void_p),('flags',c.c_uint32),('app',c.c_void_p),('layers',c.c_uint32),('layer_names',c.c_void_p),('extensions',c.c_uint32),('extension_names',c.c_void_p)]
v.vkCreateInstance.argtypes=[c.POINTER(Info),c.c_void_p,c.POINTER(c.c_void_p)]
v.vkEnumeratePhysicalDevices.argtypes=[c.c_void_p,c.POINTER(c.c_uint32),c.POINTER(c.c_void_p)]
v.vkGetPhysicalDeviceProperties.argtypes=[c.c_void_p,c.c_void_p]
v.vkDestroyInstance.argtypes=[c.c_void_p,c.c_void_p]
instance=c.c_void_p();assert v.vkCreateInstance(c.byref(Info(type=1)),None,c.byref(instance))==0
try:
 n=c.c_uint32();assert v.vkEnumeratePhysicalDevices(instance,c.byref(n),None)==0
 devices=(c.c_void_p*n.value)();assert v.vkEnumeratePhysicalDevices(instance,c.byref(n),devices)==0
 rows=[]
 for device in devices:
  data=c.create_string_buffer(4096);v.vkGetPhysicalDeviceProperties(device,data)
  fields=c.cast(data,c.POINTER(c.c_uint32));rows.append({'vendor_id':hex(fields[2]),'device_id':hex(fields[3]),'integrated':fields[4]==1,'name':data.raw[20:276].split(b'\0')[0].decode()})
 print(json.dumps(rows))
finally:v.vkDestroyInstance(instance,None)
"""
        try:
            result=self.run_gpu_probe(['--rm','--pull=never','--network','none','--read-only','--cap-drop','ALL','--security-opt','no-new-privileges','--memory','128m','--memory-swap','128m','--cpus','0.25','--pids-limit','64','--label','org.lifetech.service=gpu-inventory','--device',render,'--group-add',str(gid),'--mount','type=bind,src=/usr,dst=/usr,readonly','--tmpfs','/tmp:rw,nosuid,nodev,size=16m','--env','XDG_CACHE_HOME=/tmp','--env','VK_ICD_FILENAMES='+icd,'organization-launcher:webrtc-1','/usr/bin/python3','-c',code],timeout=15,prefix='swarm-gpu-inventory-intel-')
            rows=json.loads(result.stdout)
            if not isinstance(rows,list) or any(not isinstance(r,dict) or type(r.get('integrated')) is not bool for r in rows):raise ValueError('Invalid Vulkan physical devices')
        except (OSError,ValueError,RuntimeError,subprocess.SubprocessError):rows=[]
        cache[key]=(time.time(),rows);return rows

    def host_nvidia_allocations(self):
        if hasattr(self,'nvidia_sampler'):
            return [] if getattr(self,'cuda_runtime',None) is False else self.nvidia_sampler.snapshot()
        with self.__dict__.setdefault('nvidia_probe_lock',threading.Lock()):
            if getattr(self,'cuda_runtime',None) is False:return []
            if time.monotonic()<getattr(self,'nvidia_probe_retry_at',0):return []
            rows=self._host_nvidia_allocations()
            self.nvidia_probe_retry_at=0 if rows else time.monotonic()+60
            return rows

    def _host_nvidia_allocations(self):
        # Timestamp inside the helper: Docker startup is not sample age.
        code="import json,subprocess,time; output=subprocess.check_output(['nvidia-smi','--query-gpu=uuid,pci.bus_id,name,memory.total,memory.free,utilization.gpu,temperature.gpu','--format=csv,noheader,nounits'],text=True,timeout=5); print(json.dumps({'output':output,'sampled_at':time.time()}))"
        try:
            # Cold Docker/driver startup can exceed 15 seconds under load.
            # Freshness still starts at the measurement inside the helper.
            result=self.run_gpu_probe(['--rm','--pull=never','--network','none','--read-only','--cap-drop','ALL','--security-opt','no-new-privileges','--memory','96m','--memory-swap','96m','--cpus','0.25','--pids-limit','128','--label','org.lifetech.service=gpu-inventory','--gpus','all','--env','NVIDIA_DRIVER_CAPABILITIES=utility','organization-launcher:webrtc-1','python3','-c',code],timeout=60,prefix='swarm-gpu-inventory-nvidia-')
            value=json.loads(result.stdout)
            return [{**row,'sampled_at':value['sampled_at']} for row in nvidia_allocation_rows(value['output'])]
        except (OSError,ValueError,KeyError,TypeError,RuntimeError,subprocess.SubprocessError):return []

    def gpu_resources(self):
        with self.__dict__.setdefault('gpu_inventory_lock',threading.RLock()):return self._gpu_resources()

    def _gpu_resources(self):
        """Node-owned GPU identity/allocation detection; no application profiles."""
        now=time.time()
        if getattr(self,'gpu_resource_cache',None) is not None and now-self.gpu_resource_sampled<1:return self.gpu_resource_cache
        inventory=hardware_sample(disk=self.root,query_nvidia=False)
        if any(row.get('vendor')=='NVIDIA' and (not row.get('uuid') or row.get('memory_free_mib') is None) for row in inventory['gpus']):
            nvidia=self.host_nvidia_allocations()
            if nvidia:inventory['gpus']=[r for r in inventory['gpus'] if r['vendor']!='NVIDIA']+nvidia
        machine=hashlib.sha256(Path('/etc/machine-id').read_bytes()).hexdigest()
        memory={line.split(':',1)[0]:int(line.split()[1]) for line in Path('/proc/meminfo').read_text().splitlines() if len(line.split())>=2 and line.split()[1].isdigit()}
        rows=[];groups=None
        for raw in inventory['gpus']:
            row=dict(raw)
            if row.get('render_device') and row['vendor']!='NVIDIA':
                row['uuid']='DRM-'+hashlib.sha256((machine+row['pci_id']+row.get('vendor_id','')+row.get('device_id','')).encode()).hexdigest()[:32]
                if groups is None:groups=self.host_render_groups()
                if row.get('render_gid') is None:row['render_gid']=groups.get(Path(row['render_device']).name)
                if row['vendor']=='AMD':
                    icds=getattr(self,'gpu_host_icds',None)
                    row['vulkan_icds_known']=icds is not None
                    row['icd_candidates']=[x['path'] for x in (icds or []) if 'radeon' in x['library'].lower()]
                if row['vendor']=='Intel':
                    icds=getattr(self,'gpu_host_icds',None)
                    row['vulkan_icds_known']=icds is not None
                    row['icd_candidates']=[x['path'] for x in (icds or []) if Path(x['library']).name=='libvulkan_intel.so']
                    candidates=row['icd_candidates']
                    if len(candidates)==1 and type(row.get('render_gid')) is int:
                        devices=self.host_intel_devices(row['render_device'],row['render_gid'],candidates[0])
                        matches=[d for d in devices if d.get('vendor_id')==row.get('vendor_id') and d.get('device_id')==row.get('device_id')]
                        if len(matches)==1 and matches[0]['integrated']:
                            row.update(device_type='integrated',name=matches[0]['name'],memory_domain='shared-system-memory',memory_source='Vulkan physical integrated device; kernel host RAM',memory_total_mib=None,memory_free_mib=None,memory_used_mib=None,shared_memory_available_mib=memory.get('MemAvailable',0)/1024,gpu_allocation_added_to_host_ram=False)
            row['allocation_known']=row.get('memory_total_mib') is not None and row.get('memory_free_mib') is not None
            row.setdefault('sampled_at',now);rows.append(row)
        # Slow device classification must finish before taking the shared
        # ownership/RAM snapshot. Keep allocation timestamps on each device.
        memory={line.split(':',1)[0]:int(line.split()[1]) for line in Path('/proc/meminfo').read_text().splitlines() if len(line.split())>=2 and line.split()[1].isdigit()}
        memory_sampled=time.time()
        released=self.stream_work.released_gpu_work() if hasattr(self,'stream_work') else set()
        workers=gpu_worker_commitments(released) if released else gpu_worker_commitments()
        for row in rows:
            if row.get('memory_domain')=='shared-system-memory':row['shared_memory_available_mib']=memory.get('MemAvailable',0)/1024
            row.update(workers['devices'].get(row.get('uuid'),{'active_workers':0,'private_commitment_mib':0,'exclusive_worker_present':False}))
            row['workers_known']=workers['known']
        self.gpu_resource_cache={'machine':machine,'sampled_at':memory_sampled,'gpus':rows,
            'nvidia_sampler':self.nvidia_sampler.health() if hasattr(self,'nvidia_sampler') else None,
            'host_memory':{'total_mib':memory.get('MemTotal',0)/1024,'available_mib':memory.get('MemAvailable',0)/1024,
                           'sampled_at':memory_sampled,
                           'source':'kernel MemTotal/MemAvailable; GPU allocations are not added'},
            'selection_policy':'physical identity and measured allocation; workload qualification required separately'}
        self.gpu_resource_sampled=time.time()
        return self.gpu_resource_cache

    def hardware(self, info=None):
        with self.__dict__.setdefault('hardware_lock',threading.RLock()):return self._hardware(info)

    def _hardware(self, info=None):
        if self.hardware_cache is None or time.time()-self.hardware_sampled>15:
            self.hardware_cache=hardware_sample(disk=self.root,query_nvidia=False)
            self.hardware_sampled=time.time()
        if info is not None:self.cuda_runtime='nvidia' in info.get('Runtimes',{})
        value={**self.hardware_cache,'gpu_validation':self.gpu_validation,
               'cuda_container_runtime':getattr(self,'cuda_runtime',None)}
        try:
            state=json.loads((self.config.parent/'performance-state.json').read_text())
            if state.get('status')=='published' and 0<=time.time()-state['sampled_at']<15:
                devices=state.get('gpu_devices',{})
                value['gpus']=[{**g,**devices.get(g.get('pci_id'),{})} for g in value.get('gpus',[])]
        except (OSError,ValueError,TypeError,KeyError):pass
        # Driver/runtime probes must never hold up node readiness or CPU RPCs.
        if time.time()-getattr(self,'hardware_gpu_at',0)<20:
            rows={g['pci_id']:g for g in getattr(self,'hardware_gpu_rows',[])}
            value['gpus']=[({**rows.get(g.get('pci_id'),{}),**g} if g.get('utilization_percent') is not None else {**g,**rows.get(g.get('pci_id'),{})}) for g in value.get('gpus',[])]
        missing=any(g.get('vendor')=='NVIDIA' and g.get('utilization_percent') is None for g in value.get('gpus',[]))
        probe=getattr(self,'hardware_gpu_probe',None)
        if missing and (probe is None or not probe.is_alive()) and time.time()-getattr(self,'hardware_gpu_attempt',0)>30:
            self.hardware_gpu_attempt=time.time()
            def collect():
                rows=self.host_nvidia_allocations()
                with self.hardware_lock:
                    self.hardware_gpu_rows=rows;self.hardware_gpu_at=time.time()
            self.hardware_gpu_probe=threading.Thread(target=collect,daemon=True,name='node-gpu-counters')
            self.hardware_gpu_probe.start()
        return value

    def close(self):
        if hasattr(self,'nvidia_sampler'):self.nvidia_sampler.close()
        if hasattr(self,'commands'):self.commands.close()
        if hasattr(self,'stream_work'):self.stream_work.close()
        if hasattr(self,'control_executor'):self.control_executor.shutdown(wait=False,cancel_futures=True)
        self.telemetry.stopped.set()
        self.storage_stop.set()
        observer=getattr(self,'worker_event_process',None)
        if observer and observer.poll() is None:observer.terminate()
        thread=getattr(self,'worker_event_thread',None)
        if thread:thread.join(timeout=5)
        for entry in getattr(self,'pending_resources',{}).values():
            lease=entry['lease']
            if lease.row.get('handover_until',0)>time.time():lease.file.close()
            else:lease.close()
        self.pending_resources={}
        for timer in self.browser_timers.values():timer.cancel()
        for ident in list(self.browsers):self.remove_containers(ident)
        for lease in getattr(self,'p2p_leases',{}).values():lease.close()
        for ident,job in list(self.jobs.items()):
            if job['status']=='running':
                (self.folder(ident)/'STOP').touch()
                self.remove_containers(ident)
        if hasattr(self,'instance_lock'):self.instance_lock.close()
