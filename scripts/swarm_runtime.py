"""Shared swarm-node execution and verified transfer runtime.

No application imports, scientific algorithms, sample identities or fixed budgets.
Applications declare resources, mounts, scratch and labels. The runtime enforces
admission/isolation, observes lifecycle events, and cleans only its job container.
"""
import datetime
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
import uuid

ROOT=Path(os.environ['JSWARM_WORKSPACE']) if os.environ.get('JSWARM_WORKSPACE') else (Path(__file__).resolve().parents[2] if Path(__file__).resolve().parents[1].name in ('GenomeHarbor', 'JSwarm') else Path(__file__).resolve().parents[1])
SERVER=ROOT/'swarm-control/server'
if str(SERVER) not in sys.path:sys.path.insert(0,str(SERVER))
from agent_client import AgentClient,CONFIG


class CapacityWait(RuntimeError):
    """Execution is waiting for owner permission or reserved resources."""


class GpuUnavailable(CapacityWait):
    """A compatible GPU cannot currently execute; CPU fallback remains valid."""


def validate_gpu(gpu):
    from scripts.launcher_streams import GPU_DRM_KEYS,GPU_ID_PATTERN,GPU_OPTIONAL_KEYS
    if not isinstance(gpu,dict) or set(gpu)-{'max_concurrency','vram_mb'}-GPU_DRM_KEYS-GPU_OPTIONAL_KEYS!={'uuid','minimum_free_mib'}:
        raise ValueError('Expected an explicit GPU identity and VRAM headroom')
    if not isinstance(gpu['uuid'],str) or not re.fullmatch(GPU_ID_PATTERN,gpu['uuid']) or type(gpu['minimum_free_mib']) is not int or gpu['minimum_free_mib']<1:
        raise ValueError('Invalid physical GPU request')
    from scripts.launcher_streams import validate_gpu_sharing
    validate_gpu_sharing(gpu)


def resolve_gpu_inventory(gpu,inventory,machine):
    from scripts.launcher_streams import resolve_gpu_allocation
    try:return resolve_gpu_allocation(gpu,inventory,machine)
    except RuntimeError as error:raise GpuUnavailable(str(error)) from error


def shell_command(host,command):
    """Run locally or through the signed launcher channel, never through SSH."""
    if host['local']:return command
    if not host.get('agent_endpoint') or not host.get('machine'):
        raise ValueError('Enrolled command endpoint and machine identity required; no SSH fallback')
    return [sys.executable,str(ROOT/'scripts/swarm_command.py'),'--host',host['host'],
            '--endpoint',host['agent_endpoint'],'--machine',host['machine'],'--',*command]


def eligible(status):
    return bool(status.get('ready') and status.get('protocol')=='lifetech-agent.v1'
                and status.get('worker_policy',{}).get('accepting_jobs'))


def discover(required_image=None, names=None):
    from concurrent.futures import ThreadPoolExecutor
    from docker_utils.images import ImageResolver
    config=json.loads(CONFIG.read_text())
    resolver=ImageResolver([],lambda h:h['ssh'])
    expected=resolver.fingerprint(None,required_image) if required_image else None
    if required_image and not expected:raise RuntimeError('Approved local requested image unavailable')
    def check(item):
        name,address=item
        host={'host':name,'local':name==config['name'],'lan_address':address,'agent_endpoint':'http://'+address+':8790'}
        client=None
        try:
            client=AgentClient(host);status=client.call('status',timeout=15)
            if not eligible(status):raise RuntimeError('Agent is unavailable or declining jobs')
            if address not in status.get('addresses',[]):raise RuntimeError('Agent address attestation failed')
            host['machine']=status['machine'];host['capabilities']=status.get('capabilities',[])
            if 'node-command-v1' not in status.get('capabilities',[]):raise RuntimeError('Launcher command transport unavailable; update required')
            limits=subprocess.check_output(shell_command(host,['docker','info','--format','{{json .MemoryLimit}} {{json .SwapLimit}}']),text=True,stderr=subprocess.DEVNULL,timeout=30).split()
            if limits!=['true','true']:raise RuntimeError('Docker memory/swap isolation unavailable')
            if required_image:
                image=subprocess.check_output(shell_command(host,['docker','image','inspect',required_image,'--format','{{.Id}}']),text=True,stderr=subprocess.DEVNULL,timeout=20).strip()
                fingerprint=resolver.fingerprint(None if host['local'] else host,required_image)
                if fingerprint!=expected or not image.startswith('sha256:'):raise RuntimeError('Worker does not have the approved requested image contents')
                host['image_fingerprint']=fingerprint
                host['image']=image
            host['eligible']=True;host['machine']=status['machine'];host['cpus']=client.call('metrics',timeout=15).get('cpus') or 2
            if 'shared-resources' not in status.get('capabilities',[]):raise RuntimeError('Worker needs the shared resource scheduler update')
        except Exception as error:host.update(eligible=False,reason=type(error).__name__+': '+str(error)[:240])
        finally:
            if client:client.close()
        return host
    peers=[item for item in config['peers'].items() if names is None or item[0] in names]
    if not peers:return []
    with ThreadPoolExecutor(max_workers=min(8,len(peers))) as pool:return list(pool.map(check,peers))


def stamp():return datetime.datetime.now(datetime.timezone.utc).isoformat()


def atomic_json(path,value):
    from scripts.launcher_streams import atomic_json as durable_json
    durable_json(path,value)


def validate_job(cpus,memory_mb,group,tmpfs,labels,container_prefix):
    if type(cpus) is not int or type(memory_mb) is not int or min(cpus,memory_mb)<1:raise ValueError('Expected positive integral CPU/RAM budgets')
    if not isinstance(group,str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,128}',group):raise ValueError('Expected a job group identifier')
    if container_prefix not in (None,'genomics','swarm-job'):raise ValueError('Unsupported job container prefix')
    if tmpfs is not None and not isinstance(tmpfs,dict):raise ValueError('RAM scratch must be a mount-to-budget mapping')
    if labels is not None and not isinstance(labels,dict):raise ValueError('Application labels must be a mapping')
    total=0
    for target,size_mb in (tmpfs or {}).items():
        if not isinstance(target,str) or not re.fullmatch(r'/[a-zA-Z0-9_/-]+',target) or type(size_mb) is not int or size_mb<=0:raise ValueError('Invalid bounded RAM scratch mount')
        total+=size_mb
    if total>=memory_mb:raise ValueError('Combined RAM scratch must leave memory for the application')
    for key,value in (labels or {}).items():
        if not isinstance(key,str) or not re.fullmatch(r'[A-Za-z0-9_.-]+',key) or not isinstance(value,str):raise ValueError('Invalid application label')
        if key in ('org.lifetech.worker','org.lifetech.job','org.lifetech.group','org.lifetech.sink','org.lifetech.run','org.lifetech.agent-revision','org.lifetech.report','org.lifetech.shared-cache','org.lifetech.cache-owner'):raise ValueError('Application cannot override runtime identity labels')


def cleanup_container(host,name):
    """Prove a job container absent even if its controller transport already exited."""
    try:
        removed=subprocess.run(shell_command(host,['docker','rm','-f',name]),capture_output=True,text=True,timeout=30)
        if removed.returncode==0:return True
    except subprocess.TimeoutExpired:
        pass  # Docker may still be removing it after the command channel times out.
    deadline=time.monotonic()+120
    while time.monotonic()<deadline:
        try:
            inspected=subprocess.run(shell_command(host,['docker','container','inspect',name]),capture_output=True,text=True,timeout=20)
            if inspected.returncode==1 and ('no such object:' in inspected.stderr.lower() or 'no such container:' in inspected.stderr.lower()):return True
            if inspected.returncode==0:
                rows=json.loads(inspected.stdout)
                if len(rows)!=1 or rows[0].get('Name','').lstrip('/')!=name:return False
                item=rows[0];state=item.get('State',{})
                # Fresh inspection authorizes retirement of this exact stopped
                # instance. Never blindly repeat a force-removal by name.
                if not state.get('Running') and not state.get('Restarting') and re.fullmatch('[a-f0-9]{64}',item.get('Id','')):
                    retired=subprocess.run(shell_command(host,['docker','rm',item['Id']]),capture_output=True,text=True,timeout=20)
                    if retired.returncode==0:return True
                elif state.get('Running') or state.get('Restarting'):return False
        except (subprocess.TimeoutExpired,ValueError):
            pass
        time.sleep(1)
    return False


def observe_worker(host,name,lease,*,reconnect_seconds=180,poll_seconds=2):
    """Reattach to a Docker-owned job after node-process replacement.

    Inspections are read-only and retryable. Never relaunch an uncertain job;
    the name and immutable ownership labels must match before trusting its exit.
    """
    deadline=time.monotonic()+reconnect_seconds
    while True:
        try:
            inspected=subprocess.run(shell_command(host,['docker','inspect','--format','{{json .}}',name]),capture_output=True,text=True,timeout=20)
            if inspected.returncode:
                if 'no such object:' in inspected.stderr.lower() or 'no such container:' in inspected.stderr.lower():
                    raise ValueError('Job container disappeared: '+name)
                raise ConnectionError('Worker inspection temporarily unavailable')
            item=json.loads(inspected.stdout);labels=item.get('Config',{}).get('Labels') or {}
            if item.get('Name','').removeprefix('/')!=name or labels.get('org.lifetech.worker')!='true' or labels.get('org.lifetech.job')!=lease:
                raise ValueError('Job container ownership differs')
            state=item['State']
            if not state.get('Running') and not state.get('Restarting'):
                if state.get('Status') not in ('exited','dead'):raise ValueError('Job container has not started')
                return state['ExitCode']
            deadline=time.monotonic()+reconnect_seconds
        except (OSError,subprocess.TimeoutExpired,ConnectionError):
            if time.monotonic()>=deadline:raise RuntimeError('Worker did not reconnect after node update') from None
        time.sleep(poll_seconds)


class MissingWorker(ValueError):
    """The enrolled node confirms the named execution no longer exists."""
    def __init__(self,message,*,container=None,lease=None):
        super().__init__(message)
        self.container=container
        self.lease=lease


def observe_recorded_worker(client,host,name,lease,*,on_observed=None,reconnect_seconds=960,poll_seconds=2):
    """Resume the existing attempt through a restartable node's durable journal."""
    deadline=time.monotonic()+reconnect_seconds
    while True:
        try:
            status=client.call('status',timeout=10)
            if status.get('machine')!=host['machine']:raise ValueError('Worker machine identity changed')
            value=client.call('worker.status',{'job':lease,'container':name},timeout=20)
            if not value.get('found'):
                last=value.get('last_observation') or {}
                if last.get('exit_code') is not None:return last
                raise MissingWorker('Docker worker is missing; execution is not complete',container=name,lease=lease)
            if on_observed:on_observed(value)
            if value.get('exit_code') is not None:return value
            deadline=time.monotonic()+reconnect_seconds
        except (OSError,http.client.HTTPException,RuntimeError):
            if time.monotonic()>=deadline:raise RuntimeError('Worker did not reconnect after node replacement') from None
            client.close()
        time.sleep(poll_seconds)


def _run_job(host,image,command,mounts,work,cpus,memory_mb,report,*,group,wait_for_capacity=False,on_admitted=None,tmpfs=None,labels=None,container_prefix=None,gpu=None,network='none',shared_cache=None,memory_reservation_mb=None,profiling_context=None,gpu_compute_complete_file=None,run_id=None,shared_caches=None,pids_limit=32):
    """Docker RAM, including bounded tmpfs scratch, fits the acquired lease."""
    validate_job(cpus,memory_mb,group,tmpfs,labels,container_prefix)
    if type(pids_limit) is not int or not 32<=pids_limit<=4096:
        raise ValueError('Worker PID limit must be an integer between 32 and 4096')
    if run_id is not None and (not isinstance(run_id,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}',run_id)):
        raise ValueError('Run ID must be a bounded operational identifier')
    if profiling_context is not None and (not isinstance(profiling_context,dict) or len(json.dumps(profiling_context))>8192):
        raise ValueError('Profiling context must be a bounded JSON object')
    if gpu_compute_complete_file is not None:
        marker=Path(gpu_compute_complete_file)
        if gpu is None or not marker.is_absolute() or '..' in marker.parts or not marker.is_relative_to('/work') or str(marker)=='/work':
            raise ValueError('GPU completion marker must be a file beneath /work')
    reservation=memory_mb if memory_reservation_mb is None else memory_reservation_mb
    if type(reservation) is not int or not 1<=reservation<=memory_mb:
        raise ValueError('RAM reservation must be positive and no larger than the hard limit')
    if sum((tmpfs or {}).values())>reservation:
        raise ValueError('RAM reservation must cover private tmpfs')
    if network not in ('none','host'):raise ValueError('Expected isolated or explicit host network')
    if gpu is not None:
        validate_gpu(gpu)
        from scripts.launcher_streams import gpu_host_memory_mb
        extra=gpu_host_memory_mb(gpu);memory_mb+=extra;reservation+=extra
    shared_caches=shared_caches or {}
    targets=list(shared_caches)+(['/dev/shm'] if shared_cache else [])
    if len(set(targets))!=len(targets):raise ValueError('Duplicate shared cache mount')
    for target in targets:
        path=Path(target)
        if not path.is_absolute() or '..' in path.parts or str(path)!=target or not re.fullmatch(r'/[A-Za-z0-9_./-]+',target) or target in ('/','/work','/tmp'):raise ValueError('Invalid shared cache target')
    if any(a!=b and Path(a) in Path(b).parents for a in targets for b in targets):raise ValueError('Overlapping shared cache mounts')
    protected={c['key'] for c in shared_caches.values()}|({shared_cache['key']} if shared_cache else set())
    report=Path(report)
    lease=uuid.uuid4().hex;name=None;admission_requested=False;receipt_saved=False;queued_at=stamp()
    client=AgentClient(host);done=threading.Event();errors=[];process=None
    try:
        status=client.call('status')
        if status.get('machine')!=host['machine']:raise RuntimeError('Worker identity changed')
        host['capabilities']=status.get('capabilities',[])
        ram_only=status.get('storage_policy',{}).get('mode')=='ram-only'
        if ram_only:client.call('worker.storage.validate',{'paths':[str(source) for source,_,_ in mounts]})
        # Older enrolled agents require this legacy wire name. It does not
        # select a scientific workload; generic workers carry their own group.
        prefix=container_prefix or ('swarm-job' if 'generic-workers' in status.get('capabilities',[]) else 'genomics')
        name=prefix+'-'+lease
        if not eligible(status):raise CapacityWait('Worker is declining new work')
        if 'renewable-resources' not in status.get('capabilities',[]):raise RuntimeError('Renewable leases unavailable')
        capacity=client.call('resource.capacity',{'group':group})
        if capacity.get('available_pids',pids_limit)<pids_limit:
            raise CapacityWait('Insufficient current worker PID capacity')
        if capacity['available_mb']<reservation and ('verified-ram-caches-v1' in status.get('capabilities',[]) or any(w.get('group')=='shared-cache' for w in status.get('workers',[]))):
            from scripts.swarm_shared_cache import reclaim_completed
            if reclaim_completed(host,keep=protected,required_mb=reservation):
                capacity=client.call('resource.capacity',{'group':group})
        if not wait_for_capacity and (capacity['available_mb']<reservation or capacity.get('available_cpus',0)<cpus):raise CapacityWait('Insufficient current signed capacity')
        request={'id':lease,'group':group,'cpus':cpus,'memory_mb':reservation,'container':name}
        sink='gpu' if gpu is not None else 'cpu'
        if 'independent-job-sinks-v1' in status.get('capabilities',[]):request['sink']=sink
        admission_requested=True
        result=client.call('resource.acquire',request)
        if not wait_for_capacity and not result['acquired']:raise CapacityWait('Capacity changed before reservation')
        report.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
        last_cache_reclaim=time.monotonic()
        while not result['acquired']:
            atomic_json(report,{'phase':'waiting_capacity','host':host['host'],'sink':sink,'cpus':cpus,'memory_mb':memory_mb,'updated':stamp(),'container':name,'lease_id':lease,'image':image,'inputs_ready':True})
            time.sleep(2)
            status=client.call('status')
            if not eligible(status) or status.get('machine')!=host['machine']:raise CapacityWait('Worker is updating or declining new work')
            if 'verified-ram-caches-v1' in status.get('capabilities',[]) and time.monotonic()-last_cache_reclaim>=10:
                capacity=client.call('resource.capacity',{'group':group})
                if capacity['available_mb']<reservation:
                    from scripts.swarm_shared_cache import reclaim_completed
                    reclaim_completed(host,keep=protected,required_mb=reservation)
                last_cache_reclaim=time.monotonic()
            result=client.call('resource.acquire',request)
        # Transfers and admission waits can outlive an owner's policy change or
        # a launcher reconnect. Re-attest immediately before starting compute.
        status=client.call('status')
        if status.get('machine')!=host['machine']:raise RuntimeError('Worker identity changed before launch')
        if not eligible(status):raise CapacityWait('Worker paused or draining before launch')
        if on_admitted:on_admitted()
        client.call('resource.renew',{'id':lease})
        def renew():
            other=AgentClient(host)
            try:
                while not done.wait(60):
                    for attempt in range(3):
                        try:
                            other.call('resource.renew',{'id':lease},timeout=10)
                            break
                        except Exception as error:
                            if isinstance(error,PermissionError) or attempt==2:
                                errors.append(type(error).__name__+': '+str(error))
                                subprocess.run(shell_command(host,['docker','kill',name]),capture_output=True,timeout=20)
                                return
                            other.close();other=AgentClient(host)
                            if done.wait(5*(attempt+1)):return
            finally:other.close()
        if 'docker-workers' not in status.get('capabilities',[]):threading.Thread(target=renew,daemon=True).start()
        detached=not host['local']
        argv=['docker','run','--pull=never','--name',name,'--label','org.lifetech.worker=true','--label','org.lifetech.job='+lease,'--label','org.lifetech.group='+group,'--network',network,'--read-only','--log-driver','local' if detached else 'none','--cpus',str(cpus),'--memory',str(memory_mb)+'m','--memory-swap',str(memory_mb)+'m','--pids-limit',str(pids_limit),'--cap-drop','ALL','--security-opt','no-new-privileges','--ulimit','core=0:0','--ulimit','nofile=65536:65536','--user',host.get('container_user','1000:1000'),'-e','TMPDIR=/work/tmp','-w','/work']
        if reservation!=memory_mb:argv+=['--memory-reservation',str(reservation)+'m']
        if detached:
            argv+=['--detach']
            if ram_only:argv[argv.index('--log-driver')+1]='none'
            else:argv+=['--log-opt','max-size=8m','--log-opt','max-file=2']
        argv+=['--label','org.lifetech.sink='+sink,'--label','org.lifetech.run='+(run_id or report.stem),
                '--label','org.lifetech.accounting-file=/work/.swarm-execution-'+lease+'.json',
                '--label','org.lifetech.agent-revision='+str(status.get('source_revision','unknown')),
                '--label','org.lifetech.report='+str(report)]
        if shared_cache:
            from scripts.swarm_shared_cache import mount_args
            if '/dev/shm' in (tmpfs or {}):raise ValueError('Private tmpfs cannot shadow the shared reference')
            argv+=mount_args(host,shared_cache)
        for target,cache in shared_caches.items():
            from scripts.swarm_shared_cache import mount_args
            if target in (tmpfs or {}):raise ValueError('Private tmpfs shadows immutable cache')
            argv+=mount_args(host,cache,target=target,primary=False)
        if gpu is not None:
            if 'gpu-resource-inventory-v1' in status.get('capabilities',[]):gpu=resolve_gpu_inventory(gpu,client.call('resource.gpus'),host['machine'])
            import csv,io
            try:
                if 'gpu-resource-inventory-v1' in status.get('capabilities',[]):
                    free={gpu['uuid']:gpu['minimum_free_mib']+gpu.get('vram_mb',0)}
                elif gpu.get('memory_domain')=='shared-system-memory':raise GpuUnavailable('Integrated GPU requires node memory inventory')
                elif gpu.get('render_device'):
                    code="import pathlib; p=pathlib.Path('/sys/class/drm')/pathlib.Path(__import__('sys').argv[1]).name/'device'; print((int((p/'mem_info_vram_total').read_text())-int((p/'mem_info_vram_used').read_text()))//1024**2)"
                    probe=subprocess.run(shell_command(host,['python3','-c',code,gpu['render_device']]),capture_output=True,text=True,timeout=10)
                    free={gpu['uuid']:int(probe.stdout)} if probe.returncode==0 else {}
                else:
                    probe=subprocess.run(shell_command(host,['nvidia-smi','--query-gpu=uuid,memory.free','--format=csv,noheader,nounits']),capture_output=True,text=True,timeout=10)
                    free={row[0].strip():int(row[1].strip()) for row in csv.reader(io.StringIO(probe.stdout))} if probe.returncode==0 else {}
            except (OSError, ValueError, IndexError, subprocess.SubprocessError) as error:
                raise GpuUnavailable('GPU readiness changed before launch') from error
            if free.get(gpu['uuid'],0)<gpu['minimum_free_mib']:raise GpuUnavailable('GPU VRAM headroom changed before launch')
            locks=str(Path('/dev/shm/jswarm-gpu-locks')/host['machine']) if ram_only else str(Path(host['cache']).parent/'gpu-alignment-locks')
            # Owner command capsules can expose host RAM read-only. Provision
            # through the same Docker daemon that will bind the worker path,
            # within this worker's already acquired CPU/RAM reservation.
            parent='/dev/shm' if ram_only else str(Path(locks).parent)
            relative=str(Path(locks).relative_to(parent))
            prepare="import pathlib,os,sys\np=pathlib.Path('/gpu-parent')\nfor part in pathlib.Path(sys.argv[1]).parts:\n p=p/part\n assert not p.is_symlink()\n p.mkdir(mode=0o700,exist_ok=True)\nassert (p.stat().st_uid==os.getuid() or os.getuid()==0) and not p.stat().st_mode & 0o077"
            setup=name+'-gpu-setup'
            helper=['docker','run','--rm','--pull=never','--name',setup,'--network','none','--read-only',
                    '--cap-drop','ALL','--security-opt','no-new-privileges','--cpus',str(min(cpus,.25)),
                    '--memory',str(min(reservation,64))+'m','--memory-swap',str(min(reservation,64))+'m',
                    '--pids-limit','32','--user',host.get('container_user','1000:1000'),
                    '--mount','type=bind,src='+parent+',dst=/gpu-parent','--entrypoint','python3',image,'-c',prepare,relative]
            try:subprocess.run(shell_command(host,helper),check=True,timeout=30)
            except BaseException:
                subprocess.run(shell_command(host,['docker','rm','-f',setup]),capture_output=True,timeout=30)
                raise
            from scripts.launcher_streams import gpu_container_args
            argv+=gpu_container_args(gpu)+['--mount','type=bind,src='+locks+',dst=/gpu-locks',
                   '--label','org.lifetech.accelerator='+gpu['uuid'],'--label','org.lifetech.gpu-memory-domain='+gpu.get('memory_domain','driver-reserved-vram'),'--label','org.lifetech.gpu-admission=pressure','--label','org.lifetech.gpu-private-mib='+str(gpu.get('vram_mb',gpu['minimum_free_mib']))]
            from scripts.launcher_streams import GPU_ADMISSION_SOURCE
            command=shlex.join(['python3','-c',GPU_ADMISSION_SOURCE,json.dumps({**gpu,'lock_directory':'/gpu-locks',**({'compute_complete_file':gpu_compute_complete_file} if gpu_compute_complete_file else {})}),'bash','-o','pipefail','-c',command])
        for key,value in (labels or {}).items():argv+=['--label',key+'='+value]
        for target,size_mb in (tmpfs or {}).items():
            argv+=['--tmpfs',target+':rw,nosuid,nodev,noexec,size='+str(size_mb)+'m,mode=1777']
        for source,target,readonly in mounts:
            argv+=['--mount','type=bind,src='+str(source)+',dst='+target+(',readonly' if readonly else '')]
        from scripts.launcher_streams import EXECUTION_ACCOUNTING_SOURCE
        accounting_path='/work/.swarm-execution-'+lease+'.json'
        wrapped=shlex.join(['python3','-c',EXECUTION_ACCOUNTING_SOURCE,accounting_path,lease,'bash','-o','pipefail','-c',command])
        launch_command='if command -v python3 >/dev/null 2>&1; then exec '+wrapped+'; else exec '+shlex.join(['bash','-o','pipefail','-c',command])+'; fi'
        argv +=[image,'bash','-o','pipefail','-c',launch_command]
        report.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
        execution_started=stamp();execution_clock=time.monotonic()
        atomic_json(report,{'phase':'running','host':host['host'],'sink':sink,'container':name,'lease_id':lease,'cpus':cpus,'memory_mb':memory_mb,'memory_reservation_mb':reservation,'started':execution_started,'command':command,'image':image,'profiling_context':profiling_context,'ram_scratch_mounts':tmpfs or {},'gpu':gpu,'network':network,**({'shared_cache':shared_cache} if shared_cache else {})})
        # Long device/mount preparation must not spend an expired start permit.
        if not client.call('resource.acquire',request)['acquired']:raise CapacityWait('Launch reservation expired; waiting for current capacity')
        with report.with_suffix('.stdout').open('wb') as stdout,report.with_suffix('.stderr').open('wb') as stderr:
            process=subprocess.Popen(shell_command(host,argv),stdout=stdout,stderr=stderr)
            code=process.wait()
        observed=None
        if detached and code==0:
            if 'durable-docker-workers-v1' in status.get('capabilities',[]):
                def record_observation(value):
                    current=json.loads(report.read_text())
                    atomic_json(report,{**current,'container_id':value['container_id'],'image':value['image'],
                                        'worker_observation':value})
                observed=observe_recorded_worker(client,host,name,lease,on_observed=record_observation)
                code=observed['exit_code']
            else:code=observe_worker(host,name,lease)
            # The Docker daemon retains bounded output independently of the node.
            with report.with_suffix('.stdout').open('wb') as stdout,report.with_suffix('.stderr').open('wb') as stderr:
                subprocess.run(shell_command(host,['docker','logs',name]),stdout=stdout,stderr=stderr,timeout=30)
        if not detached and 'durable-docker-workers-v1' in status.get('capabilities',[]):
            observed=observe_recorded_worker(client,host,name,lease)
            if observed['exit_code']!=code:raise RuntimeError('Docker worker exit status differs from transport')
        detail={}
        if code or errors:
            try:
                inspected=subprocess.run(shell_command(host,['docker','inspect','--format','{{json .State}}',name]),capture_output=True,text=True,timeout=15)
                state=json.loads(inspected.stdout)
                detail={k:state.get(k) for k in ('Status','ExitCode','OOMKilled','Error','FinishedAt')}
            except (OSError,ValueError,subprocess.SubprocessError):
                detail={'inspection':'unavailable'}
        owner_paused=False
        if code or errors:
            try:owner_paused=client.call('worker.policy').get('accepting_jobs') is False
            except (OSError,RuntimeError,ValueError):pass
        result={'phase':'interrupted' if owner_paused else 'complete' if code==0 and not errors else 'failed','host':host['host'],'sink':sink,'container':name,'lease_id':lease,'exit_code':code,'started':execution_started,'elapsed_seconds':time.monotonic()-execution_clock,'finished':stamp(),'command':command,'image':image,'profiling_context':profiling_context,'cpus':cpus,'memory_mb':memory_mb,'memory_reservation_mb':reservation,'container_state':detail,'lease_errors':errors,'ram_scratch_mounts':tmpfs or {},'gpu':gpu,'network':network}
        from scripts.launcher_streams import MEMORY_ACCOUNTING_FIELDS
        accounting={k:observed[k] for k in ('started','finished','elapsed_seconds','cpu_core_seconds','cpu_sampled_at','cpu_throttled_seconds','cpu_measurement_basis','cpu_accounting_coverage',*MEMORY_ACCOUNTING_FIELDS) if observed and k in observed}
        if not accounting and host.get('local'):
            from scripts.launcher_streams import accounting_fields
            try:accounting=accounting_fields(json.loads((Path(work)/Path(accounting_path).name).read_text()),lease)
            except (OSError,ValueError,TypeError,KeyError):pass
        result.update(accounting)
        result.setdefault('cpu_accounting_coverage','unavailable')
        result.setdefault('memory_accounting_coverage','unavailable')
        result['admission_started']=queued_at
        result['admission_wait_seconds']=max(0,(datetime.datetime.fromisoformat(result['started'])-datetime.datetime.fromisoformat(queued_at)).total_seconds())
        if observed:result.update(container_id=observed['container_id'],image=observed['image'],worker_observation=observed)
        attempt=report.parent/'attempts'/(report.stem+'-'+name)
        if shared_cache:result['shared_cache']=shared_cache
        attempt.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
        result['attempt_report']=str(attempt.with_suffix('.json'))
        atomic_json(attempt.with_suffix('.json'),result)
        for suffix in ('.stdout','.stderr'):
            shutil.copyfile(report.with_suffix(suffix),attempt.with_suffix(suffix))
        atomic_json(report,result)
        receipt_saved=True
        if code or errors:
            # Owner stop/pause is a scheduling event, not a bad input or failed assay.
            if owner_paused:
                raise CapacityWait('Owner paused worker; requeue interrupted stage')
            if gpu is not None:
                diagnostic=report.with_suffix('.stderr').read_text(errors='replace')[-4000:]
                unavailable=code==75 or any(word in diagnostic.lower() for word in ('cudaerrormemoryallocation','cuda_error_out_of_memory','cudaerrorinsufficientdriver','cudaerrornodevice','no cuda-capable','cuda driver version is insufficient','nvidia-container-cli','could not select device driver'))
                if unavailable and not detail.get('OOMKilled'):raise GpuUnavailable('GPU backend unavailable; evidence '+str(attempt.with_suffix('.json')))
            raise RuntimeError('Worker stage failed: exit='+str(code)+' OOM='+str(detail.get('OOMKilled','unknown'))+'; evidence '+str(attempt.with_suffix('.json')))
    finally:
        done.set()
        cleaned=process is None
        try:
            if process is not None and receipt_saved:cleaned=cleanup_container(host,name)
            if cleaned and admission_requested:client.call('resource.release',{'id':lease})
            elif cleaned:pass
            else:raise RuntimeError('Container cleanup unconfirmed; resource lease retained until authenticated expiry cleanup')
        finally:client.close()



def run_job(*args, **kwargs):
    """Pin managed input objects from admission until Docker cleanup completes."""
    from contextlib import ExitStack
    from scripts.swarm_shared_cache import pin, is_managed
    host = args[0] if args else kwargs['host']
    caches = list((kwargs.get('shared_caches') or {}).values())
    if kwargs.get('shared_cache'):caches.append(kwargs['shared_cache'])
    with ExitStack() as stack:
        for cache in {c['key']:c for c in caches if is_managed(c)}.values():
            stack.enter_context(pin(host, cache))
        return _run_job(*args, **kwargs)


# Keep the public execution signature available to tooling and callers.
import functools as _functools
run_job = _functools.wraps(_run_job)(run_job)

def copy_verified_file(source,destination,receipt,resume=False):
    """Prefer direct mutual TLS for large objects; retain a verified encrypted launcher-pipe fallback."""
    if source['host']==destination['host']:return receipt
    client=AgentClient(source)
    try:
        status=client.call('status')
        if status.get('machine')!=source['machine']:raise RuntimeError('Source machine identity changed')
    finally:client.close()
    from scripts.swarm_bulk import DEFAULT_MIN_BYTES
    resume=resume or receipt['bytes']>=DEFAULT_MIN_BYTES
    target=str(Path(destination['cache'])/'incoming'/receipt['sha256']/Path(receipt['path']).name)
    offset=0
    if resume:
        probe="import pathlib,sys;p=pathlib.Path(sys.argv[1]);print(p.stat().st_size if p.exists() else 0)"
        offset=int(subprocess.check_output(shell_command(destination,['python3','-c',probe,target+'.partial']),text=True,timeout=30))
        if not 0<=offset<=receipt['bytes']:raise ValueError('Invalid partial transfer size')
    writer="""import fcntl,hashlib,json,os,pathlib,sys,time
path=pathlib.Path(sys.argv[1]);size=int(sys.argv[2]);expected=sys.argv[3]
metadata=json.loads(sys.argv[5]) if len(sys.argv)>5 else {};started=time.monotonic();last=started;last_bytes=0
progress=pathlib.Path.home()/'.local/state/lifetech-launcher/transfers'/metadata.get('id','unattributed')
progress=progress.with_suffix('.json')
try:progress.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
except OSError:progress=None
def publish(status,count,offset=0,force=False):
 global last,last_bytes
 now=time.monotonic();sent=max(0,count-offset)
 if progress is None or not force and now-last<1:return
 elapsed=max(.000001,now-started)
 row={**metadata,'status':status,'sampled_at':time.time(),'completed_bytes':count,'total_bytes':size,'transferred_bytes':sent,'bytes_per_second':max(0,sent-last_bytes)/max(.000001,now-last) if status=='running' else 0,'average_bytes_per_second':sent/elapsed,'elapsed_seconds':elapsed}
 try:
  tmp=progress.with_suffix('.tmp');tmp.write_text(json.dumps(row));tmp.chmod(0o600);tmp.replace(progress)
 except OSError:return
 last=now;last_bytes=sent
publish('preparing',0,force=True)
path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
transfer_lock=(path.parent/'.transfer.lock').open('a');fcntl.flock(transfer_lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
verified=path.with_name(path.name+'.verified.json')
def identity():
 s=path.stat();return [s.st_dev,s.st_ino,s.st_size,s.st_mtime_ns,s.st_ctime_ns]
def seal():
 t=verified.with_suffix('.tmp');t.write_text(json.dumps({'sha256':expected,'bytes':size,'identity':identity()}));t.chmod(0o600);t.replace(verified)
if path.exists() and verified.exists():
 try:
  proof=json.loads(verified.read_text())
  if proof=={'sha256':expected,'bytes':size,'identity':identity()}:publish('cached',size,size,force=True);sys.exit(0)
 except (OSError,ValueError):pass
if path.exists() and path.stat().st_size==size:
 h=hashlib.sha256()
 with path.open('rb') as s:
  for block in iter(lambda:s.read(8388608),b''):h.update(block)
 if h.hexdigest()==expected:seal();publish('cached',size,size,force=True);sys.exit(0)
# Cache probe must finish before the source stream is opened.
if len(sys.argv)>6 and sys.argv[6]=='probe':sys.exit(42)
partial=path.with_suffix(path.suffix+'.partial');h=hashlib.sha256();n=0;offset=int(sys.argv[4])
if offset:
 if not partial.exists() or partial.stat().st_size!=offset:raise RuntimeError('Partial transfer changed before resume')
 with partial.open('rb') as prefix:
  for block in iter(lambda:prefix.read(8388608),b''):h.update(block);n+=len(block)
started=last=time.monotonic()
publish('running',n,offset,force=True)
with partial.open('ab' if offset else 'wb') as s:
 while block:=sys.stdin.buffer.read(1048576):
  n+=len(block)
  if n>size:raise RuntimeError('Read transfer exceeded declared size')
  h.update(block);s.write(block);publish('running',n,offset)
if n!=size:
 publish('interrupted',n,offset,force=True)
 raise ConnectionError('Read transfer incomplete; partial retained for final full-checksum validation on resume')
if h.hexdigest()!=expected:
 publish('failed',n,offset,force=True)
 partial.rename(partial.with_name(partial.name+'.mismatch-'+str(time.time_ns())))
 raise RuntimeError('Read transfer SHA256 mismatch')
partial.chmod(0o600);partial.replace(path);seal()
publish('complete',n,offset,force=True)
# Keep bounded recent operational receipts; never retain source paths here.
if progress is not None:
 try:
  for old in sorted(progress.parent.glob('*.json'),key=lambda p:p.stat().st_mtime)[:-512]:old.unlink(missing_ok=True)
 except OSError:pass
"""
    metadata=json.dumps({'id':uuid.uuid4().hex,'task_id':receipt.get('task_id') if isinstance(receipt.get('task_id'),str) and re.fullmatch(r'[A-Za-z0-9_-]{1,100}',receipt['task_id']) else None,'source':source['host'],'destination':destination['host']})
    writer_args=['python3','-c',writer,target,str(receipt['bytes']),receipt['sha256'],str(offset),metadata]
    cached=subprocess.run(shell_command(destination,writer_args+['probe']),stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
    if cached.returncode==0:
        return {**receipt,'host':destination['host'],'path':target,'original_host':source['host']}
    if cached.returncode!=42:
        diagnostic=cached.stderr.decode(errors='replace')[-1000:]
        if 'BlockingIOError' in diagnostic or '[Errno 11]' in diagnostic:
            raise CapacityWait('A verified transfer still owns this destination object; retry without duplicating its writer')
        raise RuntimeError('LAN cache verification failed: '+diagnostic[-300:])
    # Cache hits stay local; bulk bytes use authenticated direct HTTPS by default.
    from scripts.swarm_bulk import try_copy
    transport=try_copy(source,destination,receipt,target,metadata,shell_command)
    if transport:
        return {**receipt,'host':destination['host'],'path':target,'original_host':source['host'],'transport':transport}
    # HTTPS and launcher pipes share the resume file. Re-probe after a partial HTTPS move.
    if resume:
        probe="import fcntl,pathlib,sys;p=pathlib.Path(sys.argv[1]);lock=(p.parent/'.transfer.lock').open('a');fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB);q=p.parent/'.rsync-partial'/p.name;t=pathlib.Path(str(p)+'.partial');q.replace(t) if q.exists() and not t.exists() else None;print(t.stat().st_size if t.exists() else 0)"
        offset=int(subprocess.check_output(shell_command(destination,['python3','-c',probe,target]),text=True,timeout=30))
        if not 0<=offset<=receipt['bytes']:raise ValueError('Invalid partial transfer size')
        writer_args[6]=str(offset)
    reader="import shutil,sys;s=open(sys.argv[1],'rb');s.seek(int(sys.argv[2]));shutil.copyfileobj(s,sys.stdout.buffer,8388608)"
    source_command=['python3','-c',reader,receipt['path'],str(offset)] if offset else ['cat','--',receipt['path']]
    producer=subprocess.Popen(shell_command(source,source_command),stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
    consumer=None
    try:
        consumer=subprocess.Popen(shell_command(destination,writer_args),stdin=producer.stdout,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
        producer.stdout.close()
        # Large verified files can take hours on a healthy LAN. Authenticated channel retries
        # detect broken transports; total elapsed time is not a transfer failure.
        _,errors=consumer.communicate()
        # A valid cached destination may exit before consuming a duplicate stream.
        producer.wait(timeout=20)
        if consumer.returncode:raise RuntimeError('LAN read transfer failed: '+errors.decode(errors='replace')[-300:])
    finally:
        for process in (consumer,producer):
            if process is not None:
                if process.poll() is None:
                    process.terminate()
                    try:process.wait(timeout=5)
                    except subprocess.TimeoutExpired:process.kill();process.wait()
                if process.stderr:process.stderr.close()
    return {**receipt,'host':destination['host'],'path':target,'original_host':source['host']}


def stream_command(source,source_command,destination,destination_command):
    """Pipe a trusted job input over pinned SSH without a staging file.

    The receiving application must bound and verify the stream before using it.
    Its stdout is an operational result, never the input payload.
    """
    producer=subprocess.Popen(shell_command(source,source_command),stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
    consumer=None
    try:
        consumer=subprocess.Popen(shell_command(destination,destination_command),stdin=producer.stdout,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
        producer.stdout.close()
        output,errors=consumer.communicate()
        producer.wait(timeout=20)
        if consumer.returncode or producer.returncode:
            raise RuntimeError('Streamed worker input failed: '+errors.decode(errors='replace')[-300:])
        return output
    finally:
        for process in (consumer,producer):
            if process is not None:
                if process.poll() is None:
                    process.terminate()
                    try:process.wait(timeout=5)
                    except subprocess.TimeoutExpired:process.kill();process.wait()
                if process.stderr:process.stderr.close()
