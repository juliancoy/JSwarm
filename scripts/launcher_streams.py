"""Launcher-owned, durable streaming work. No task accepts a shell command.

Built-ins hash whole immutable objects or explicit raw-byte intervals. Applications
supply valid partition boundaries and may install fixed, immutable-image recipes
in the node-owned stream-recipes.json registry. Payloads stay in bounded buffers.
"""
import contextlib
import fcntl
import gzip
import hashlib
import http.client
import io
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import ssl
import subprocess
import sys
import threading
import time

VERSION='launcher-stream-work.v1'
BUFFER=8*1024*1024
BUILTINS=('gzip.sha256.v1','bytes.sha256.v1')
TERMINAL=('completed','failed_integrity','failed','cancelled')


def canonical(value):return json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False)
def digest(value):return hashlib.sha256(canonical(value).encode()).hexdigest()
def is_sha(value):return isinstance(value,str) and re.fullmatch(r'[a-f0-9]{64}',value) is not None


def atomic_json(path,value):
    path=Path(path);path.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
    temporary=path.with_name(path.name+'.'+secrets.token_hex(6)+'.tmp')
    with temporary.open('x') as stream:
        stream.write(canonical(value));stream.flush();os.fsync(stream.fileno())
    temporary.chmod(0o600);os.replace(temporary,path)
    fd=os.open(str(path.parent),os.O_RDONLY|os.O_DIRECTORY)
    try:os.fsync(fd)
    finally:os.close(fd)


def recipes(config):
    path=Path(config).parent/'stream-recipes.json'
    extra=json.loads(path.read_text()) if path.exists() else {}
    if not isinstance(extra,dict):raise ValueError('Invalid installed stream recipes')
    for name,row in extra.items():
        if not isinstance(name,str) or name in BUILTINS or not re.fullmatch(r'[A-Za-z0-9_.-]{1,100}',name) or not isinstance(row,dict) or set(row)-{'image','argv','version','mounts','gpu','shared_cache'} or not {'image','argv','version'}<=set(row) or not isinstance(row.get('image'),str) or not re.fullmatch(r'sha256:[a-f0-9]{64}',row.get('image','')) or not isinstance(row['argv'],list) or not row['argv'] or not all(isinstance(s,str) and s and '\0' not in s for s in row['argv']) or not is_sha(row.get('version')):
            raise ValueError('Invalid installed immutable stream recipe')
        validate_execution(row)
        mounts=row.get('mounts',[])
        if not isinstance(mounts,list):raise ValueError('Invalid owner-installed mounts')
        for mount in mounts:
            if not isinstance(mount,dict) or set(mount)!={'source','target','readonly'} or type(mount['readonly']) is not bool or not all(isinstance(mount[k],str) and re.fullmatch(r'/[A-Za-z0-9_./-]+',mount[k]) and '..' not in Path(mount[k]).parts for k in ('source','target')) or any(Path(mount['target']).is_relative_to(p) or Path(p).is_relative_to(mount['target']) for p in ('/work','/runtime','/tls','/cache')):raise ValueError('Invalid owner-installed mounts')
    return extra



GPU_ADMISSION_SOURCE=r'''import contextlib,fcntl,hashlib,json,pathlib,subprocess,sys

def acquire_gpu(gpu):
    stack=contextlib.ExitStack()
    try:
        root=pathlib.Path(gpu['lock_directory']);uuid=gpu['uuid'];maximum=gpu.get('max_concurrency',1)
        if gpu.get('render_device'):
            device=pathlib.Path('/sys/class/drm')/pathlib.Path(gpu['render_device']).name/'device'
            if device.resolve().name!=gpu['pci_id'] or (device/'vendor').read_text().strip()!='0x1002':raise ValueError('AMD physical device identity changed')
            for name,sha in gpu['runtime_files'].items():
                if hashlib.sha256((pathlib.Path('/gpu-runtime')/name).read_bytes()).hexdigest()!=sha:raise ValueError('GPU runtime artifact integrity differs')
            gpu_free=(int((device/'mem_info_vram_total').read_text())-int((device/'mem_info_vram_used').read_text()))//1024**2
            if gpu_free<gpu['minimum_free_mib']+gpu.get('vram_mb',0):raise BlockingIOError('AMD VRAM headroom unavailable')
        common=stack.enter_context((root/(uuid+'.lock')).open('a'))
        fcntl.flock(common,(fcntl.LOCK_SH if maximum>1 else fcntl.LOCK_EX)|fcntl.LOCK_NB)
        if maximum==1:return stack
        with (root/(uuid+'.admission.lock')).open('a') as admission:
            fcntl.flock(admission,fcntl.LOCK_EX)
            paths={root/(uuid+'.slot-'+str(i)+'.lock') for i in range(maximum)}|set(root.glob(uuid+'.slot-*.lock'))
            chosen=None;occupied=[]
            for path in sorted(paths):
                handle=path.open('a+')
                try:fcntl.flock(handle,fcntl.LOCK_EX|fcntl.LOCK_NB)
                except BlockingIOError:
                    try:
                        handle.seek(0);occupied.append(json.load(handle))
                    finally:handle.close()
                    continue
                index=int(path.name.split('.slot-')[1].split('.')[0])
                if chosen is None and index<maximum:chosen=stack.enter_context(handle)
                else:handle.close()
            if chosen is None or len(occupied)>=min([maximum]+[r['max_concurrency'] for r in occupied]):raise BlockingIOError('GPU concurrency slots occupied')
            rows=[] if gpu.get('render_device') else subprocess.check_output(['nvidia-smi','--query-gpu=uuid,memory.free','--format=csv,noheader,nounits'],text=True,timeout=10).splitlines()
            free={uuid:gpu_free} if gpu.get('render_device') else {r.split(',')[0].strip():int(r.split(',')[1]) for r in rows}
            required=gpu['minimum_free_mib']+gpu['vram_mb']+sum(r['vram_mb'] for r in occupied)
            if free.get(uuid,0)<required:raise BlockingIOError('GPU VRAM headroom unavailable')
            chosen.seek(0);chosen.truncate();json.dump({'vram_mb':gpu['vram_mb'],'max_concurrency':maximum},chosen);chosen.flush()
        return stack
    except BaseException:stack.close();raise

if __name__=='__main__':
    try:
        with acquire_gpu(json.loads(sys.argv[1])):
            raise SystemExit(subprocess.call(sys.argv[2:]))
    except (BlockingIOError,subprocess.SubprocessError,OSError):raise SystemExit(75)
'''

GPU_DRM_KEYS={'render_device','render_gid','pci_id','icd','runtime_directory','runtime_files'}
GPU_ID_PATTERN=r'(?:GPU-[a-fA-F0-9-]{36}|DRM-[a-f0-9]{32})'


def validate_drm_gpu(gpu):
    present=GPU_DRM_KEYS & set(gpu)
    if not present:
        if str(gpu.get('uuid','')).startswith('DRM-'):raise ValueError('DRM identity requires device binding')
        return
    if present!=GPU_DRM_KEYS or not str(gpu.get('uuid','')).startswith('DRM-'):raise ValueError('Incomplete DRM GPU binding')
    if not re.fullmatch(r'/dev/dri/renderD[0-9]+',str(gpu['render_device'])) or type(gpu['render_gid']) is not int or gpu['render_gid']<0:raise ValueError('Invalid DRM render device')
    if not re.fullmatch(r'[a-fA-F0-9]{4}:[a-fA-F0-9]{2}:[a-fA-F0-9]{2}\.[0-9]',str(gpu['pci_id'])):raise ValueError('Invalid GPU PCI identity')
    if not re.fullmatch(r'/usr/share/vulkan/icd.d/[A-Za-z0-9_.-]+\.json',str(gpu['icd'])):raise ValueError('Invalid Vulkan ICD binding')
    if not re.fullmatch(r'/[A-Za-z0-9_./-]+',str(gpu['runtime_directory'])) or '..' in Path(gpu['runtime_directory']).parts:raise ValueError('Invalid GPU runtime path')
    files=gpu['runtime_files']
    if not isinstance(files,dict) or not {'bwa','exact-extension.spv'}<=set(files) or set(files)-{'bwa','exact-extension.spv','cpu-bwa'} or any(not is_sha(v) for v in files.values()):raise ValueError('Invalid GPU runtime manifest')


def resolve_gpu_allocation(gpu,inventory,machine):
    """Resolve only a qualified identity, using the node's actual allocations."""
    if inventory.get('machine')!=machine:raise RuntimeError('GPU inventory machine identity changed')
    sampled=inventory.get('sampled_at')
    if not isinstance(sampled,(int,float)) or not -5<=time.time()-sampled<=10:raise RuntimeError('GPU inventory is stale')
    matches=[row for row in inventory.get('gpus',[]) if row.get('uuid')==gpu['uuid']]
    if len(matches)!=1:raise RuntimeError('Qualified physical GPU is absent or ambiguous')
    row=matches[0]
    if row.get('workers_known') is False:raise RuntimeError('GPU worker ownership is unknown')
    if row.get('exclusive_worker_present') or row.get('active_workers',0)>=gpu.get('max_concurrency',1):raise RuntimeError('Physical GPU concurrency slots are occupied')
    free=row.get('memory_free_mib')
    if not row.get('allocation_known') or not isinstance(free,(int,float)):raise RuntimeError('GPU allocation is unknown; host RAM is not a VRAM substitute')
    if free<gpu['minimum_free_mib']+gpu.get('vram_mb',0)+row.get('private_commitment_mib',0):raise RuntimeError('Node reports insufficient GPU allocation headroom')
    result=dict(gpu)
    if gpu.get('render_device'):
        if row.get('pci_id')!=gpu['pci_id'] or row.get('vendor')!='AMD' or row.get('memory_domain')!='driver-reserved-vram':raise RuntimeError('Qualified AMD device or memory domain changed')
        if row.get('render_device') is None or type(row.get('render_gid')) is not int:raise RuntimeError('Node cannot attest the render device permissions')
        result.update(render_device=row['render_device'],render_gid=row['render_gid'])
    return result


def gpu_container_args(gpu):
    if not gpu.get('render_device'):return ['--gpus','device='+gpu['uuid'],'--env','NVIDIA_DRIVER_CAPABILITIES=graphics,compute,utility']
    validate_drm_gpu(gpu)
    return ['--device',gpu['render_device'],'--group-add',str(gpu['render_gid']),
            '--mount','type=bind,src=/usr,dst=/usr,readonly',
            '--mount','type=bind,src='+gpu['runtime_directory']+',dst=/gpu-runtime,readonly',
            '--env','PATH=/gpu-runtime:/usr/local/bin:/usr/bin:/bin',
            '--env','BWA_VULKAN_SHADER=/gpu-runtime/exact-extension.spv','--env','VK_ICD_FILENAMES='+gpu['icd']]


def validate_gpu_sharing(gpu):
    validate_drm_gpu(gpu)
    if ('max_concurrency' in gpu)!=('vram_mb' in gpu):raise ValueError('GPU sharing needs concurrency and VRAM budgets')
    if 'max_concurrency' in gpu and (type(gpu['max_concurrency']) is not int or not 1<=gpu['max_concurrency']<=32 or type(gpu['vram_mb']) is not int or gpu['vram_mb']<1):raise ValueError('Invalid GPU sharing budget')

def validate_execution(definition):
    gpu=definition.get('gpu')
    if gpu is not None:
        if (not isinstance(gpu,dict) or set(gpu)-{'max_concurrency','vram_mb'}-GPU_DRM_KEYS!={'uuid','minimum_free_mib','lock_directory'}
                or not re.fullmatch(GPU_ID_PATTERN,str(gpu.get('uuid','')))
                or type(gpu.get('minimum_free_mib')) is not int or gpu['minimum_free_mib']<1
                or not re.fullmatch(r'/[A-Za-z0-9_./-]+',str(gpu.get('lock_directory','')))
                or '..' in Path(gpu['lock_directory']).parts):raise ValueError('Invalid installed GPU binding')
        validate_gpu_sharing(gpu)
    cache=definition.get('shared_cache')
    if cache is not None:
        if not isinstance(cache,dict) or set(cache)!={'image','files','key','container','volume','memory_mb','shm_mb'}:raise ValueError('Invalid shared reference binding')
        if not re.fullmatch(r'sha256:[a-f0-9]{64}',str(cache['image'])) or not isinstance(cache['files'],dict) or not cache['files']:raise ValueError('Invalid reference owner image/objects')
        for name,row in cache['files'].items():
            if not re.fullmatch(r'[A-Za-z0-9_-][A-Za-z0-9_.-]{0,119}',name) or not isinstance(row,dict) or set(row)!={'bytes','sha256'} or type(row['bytes']) is not int or row['bytes']<1 or not is_sha(row['sha256']):raise ValueError('Invalid shared reference object')
        key=digest({k:cache[k] for k in ('image','files')})
        if cache['key']!=key or cache['container']!='swarm-job-'+key[:32] or cache['volume']!='swarm-cache-'+key[:32]:raise ValueError('Shared reference identity differs')
        required=(sum(v['bytes'] for v in cache['files'].values())+1024**2-1)//1024**2+16
        if type(cache['shm_mb']) is not int or cache['shm_mb']!=required or type(cache['memory_mb']) is not int or cache['memory_mb']<required:raise ValueError('Shared reference budget differs')


def validate(request,registry=None):
    registry=registry or {}
    if not isinstance(request,dict) or set(request)-{'id','recipe','image','inputs','parameters','resources','buffer_bytes','label','namespace','storage','output','priority'}:raise ValueError('Invalid streaming work declaration')
    if not isinstance(request.get('id'),str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}',request['id']):raise ValueError('Invalid stream work ID')
    output=request.get('output')
    if output is not None and (not isinstance(output,dict) or set(output)!={'peer','machine','grant','job_key'} or not isinstance(output['peer'],str) or not all(is_sha(output[k]) for k in ('machine','grant','job_key'))):raise ValueError('Invalid enrolled output grant')
    recipe=request.get('recipe')
    if recipe not in BUILTINS and recipe not in registry:raise ValueError('Stream recipe is not installed')
    if not isinstance(request.get('image'),str) or not re.fullmatch(r'sha256:[a-f0-9]{64}',request['image']):raise ValueError('Streaming work requires a pinned local image')
    if recipe in registry and request['image']!=registry[recipe]['image']:raise ValueError('Image differs from installed recipe')
    inputs=request.get('inputs')
    if not isinstance(inputs,dict) or not inputs or not all(isinstance(k,str) and re.fullmatch(r'[A-Za-z0-9_-]{1,80}',k) for k in inputs):raise ValueError('Named immutable input descriptors required')
    for row in inputs.values():
        if not isinstance(row,dict) or set(row)-{'peer','machine','object','sha256','bytes','start','length','mode'} or not isinstance(row.get('peer'),str) or not is_sha(row.get('machine')) or not is_sha(row.get('object')) or not is_sha(row.get('sha256')) or type(row.get('bytes')) is not int or row['bytes']<0:raise ValueError('Invalid enrolled immutable input')
        if row.get('mode','stream') not in ('stream','shared'):raise ValueError('Invalid input cache preference')
        start=row.get('start',0);length=row.get('length',row['bytes']-start)
        if type(start) is not int or type(length) is not int or min(start,length)<0 or start+length>row['bytes']:raise ValueError('Input interval outside immutable object')
        if recipe=='gzip.sha256.v1' and (row['bytes']==0 or start!=0 or length!=row['bytes']):raise ValueError('Gzip work needs a complete member-aligned object; use an indexed application recipe for windows')
    if type(request.get('priority',0)) is not int or not -1000<=request.get('priority',0)<=1000:raise ValueError('Priority must be an integer from -1000 to 1000')
    resources=request.get('resources',{'cpus':1,'memory_mb':256})
    if not isinstance(resources,dict) or set(resources)!={'cpus','memory_mb'} or any(type(resources[k]) is not int or resources[k]<1 for k in resources):raise ValueError('Explicit positive CPU/RAM budgets required')
    storage=request.get('storage')
    if storage is not None:
        if (not isinstance(storage,dict) or set(storage)!={'mode','scratch_mb'} or storage['mode']!='ram-only'
                or type(storage['scratch_mb']) is not int or storage['scratch_mb']<1
                or storage['scratch_mb']+max(1,resources['memory_mb']//8)+1>=resources['memory_mb']):
            raise ValueError('RAM-only scratch must fit the declared memory budget with application headroom')
        if any(row.get('mode','stream')!='stream' for row in inputs.values()):
            raise ValueError('RAM-only work requires streamed inputs; disk cache is forbidden')
    buffer=request.get('buffer_bytes',BUFFER)
    if type(buffer) is not int or buffer<1 or buffer*8+32*1024**2>resources['memory_mb']*1024**2:raise ValueError('Streaming buffers do not fit reserved RAM')
    parameters=request.get('parameters',{})
    if not isinstance(parameters,dict):raise ValueError('Recipe parameters must be JSON metadata')
    if recipe in BUILTINS:
        if set(inputs)!={'data'} or set(parameters)-{'expected_bytes','expected_sha256'}:raise ValueError('Invalid digest recipe parameters')
        if 'expected_bytes' in parameters and (type(parameters['expected_bytes']) is not int or parameters['expected_bytes']<0):raise ValueError('Invalid expected decoded byte count')
        if 'expected_sha256' in parameters and not is_sha(parameters['expected_sha256']):raise ValueError('Invalid expected content digest')
    for key in ('namespace','label'):
        if key in request and (not isinstance(request[key],str) or '\0' in request[key]):raise ValueError('Invalid task metadata')
    return {**request,'parameters':parameters,'resources':resources,'buffer_bytes':buffer,'namespace':request.get('namespace','stream-work')}


class RangeReader(io.RawIOBase):
    """Immutable range reader using only enrolled peer addresses and certificate pins."""
    def __init__(self,obj,peer,tls,port,buffer_bytes=BUFFER):
        self.obj=obj;self.peer=peer;self.offset=0;self.cache=b'';self.cache_start=0;self.connection=None;self.transferred=0;self.port=port;self.buffer=buffer_bytes
        self.context=ssl.create_default_context(cafile=str(Path(tls)/'ca.crt'))
        self.context.minimum_version=ssl.TLSVersion.TLSv1_3
        self.context.load_cert_chain(str(Path(tls)/'identity.crt'),str(Path(tls)/'identity.key'))
        self.context.check_hostname=False  # Exact enrolled certificate pin, independent of DHCP.
    def readable(self):return True
    def seekable(self):return True
    def tell(self):return self.offset
    def seek(self,offset,whence=0):
        target=offset if whence==0 else self.offset+offset if whence==1 else self.obj['bytes']+offset if whence==2 else -1
        if not 0<=target<=self.obj['bytes']:raise ValueError('Stream seek outside immutable object')
        self.offset=target;return target
    def connect(self):
        if self.connection is None:
            connection=http.client.HTTPSConnection(self.peer['address'],self.port,context=self.context,timeout=60);connection.connect()
            if hashlib.sha256(connection.sock.getpeercert(binary_form=True)).hexdigest()!=self.peer['certificate_sha256']:
                connection.close();raise ValueError('Enrolled source certificate differs')
            self.connection=connection
    def _part(self,size):
        if not self.cache_start<=self.offset<self.cache_start+len(self.cache):
            self.connect();start=self.offset;end=min(self.obj['bytes'],start+self.buffer)-1
            try:
                self.connection.request('GET','/objects/'+self.obj['object'],headers={'Range':f'bytes={start}-{end}','If-Match':'"'+self.obj['sha256']+'"'})
                response=self.connection.getresponse()
                if response.status!=206 or response.getheader('Content-Range')!=f'bytes {start}-{end}/{self.obj["bytes"]}' or response.getheader('ETag')!='"'+self.obj['sha256']+'"':raise ValueError('Immutable source range or identity rejected')
                data=response.read(end-start+1)
                if len(data)!=end-start+1:raise ConnectionError('Truncated authenticated source range')
            except (OSError,http.client.HTTPException):
                self.connection.close();self.connection=None;raise
            self.cache=data;self.cache_start=start;self.transferred+=len(data)
        data=self.cache[self.offset-self.cache_start:self.offset-self.cache_start+size];self.offset+=len(data);return data
    def read(self,size=-1):
        remaining=min(self.buffer if size<0 else size,self.obj['bytes']-self.offset,self.buffer);pieces=[]
        while remaining:
            data=self._part(remaining)
            if not data:raise ConnectionError('Authenticated stream ended before declared size')
            pieces.append(data);remaining-=len(data)
        return b''.join(pieces)
    def readinto(self,buffer):
        data=self.read(len(buffer));buffer[:len(data)]=data;return len(data)
    def close(self):
        if self.connection:self.connection.close();self.connection=None
        super().close()


class InputWindow(io.RawIOBase):
    def __init__(self,raw,start,length):self.raw=raw;self.start=start;self.length=length;self.offset=0;raw.seek(start)
    def readable(self):return True
    def seekable(self):return True
    def tell(self):return self.offset
    def seek(self,n,whence=0):
        target=n if whence==0 else self.offset+n if whence==1 else self.length+n if whence==2 else -1
        if not 0<=target<=self.length:raise ValueError('Read outside assigned input interval')
        self.offset=target;self.raw.seek(self.start+target);return target
    def read(self,n=-1):
        n=self.length-self.offset if n<0 else min(n,self.length-self.offset)
        data=self.raw.read(min(n,getattr(self.raw,'buffer',BUFFER)));self.offset+=len(data);return data
    def readinto(self,b):
        data=self.read(len(b));b[:len(data)]=data;return len(data)
    def close(self):self.raw.close();super().close()


class CachedReader:
    def __init__(self,path,lock,transferred=0):
        self.path=path;self.lock=lock;self.transferred=transferred;self.file=path.open('rb');self.before=file_identity(path)
    def read(self,n=-1):return self.file.read(n)
    def seek(self,*args):return self.file.seek(*args)
    def tell(self):return self.file.tell()
    def fileno(self):return self.file.fileno()
    def close(self):
        if self.file.closed:return
        try:changed=file_identity(self.path)!=self.before
        finally:self.file.close();self.lock.close()
        if changed:raise ValueError('Shared immutable input changed while in use')


def file_identity(path):
    s=Path(path).stat();return [s.st_dev,s.st_ino,s.st_size,s.st_mtime_ns,s.st_ctime_ns]


def shared_input(row,create,cache,budget):
    """One SHA-verified copy per node, protected against eviction while being read."""
    import shutil
    cache=Path(cache);cache.mkdir(mode=0o700,parents=True,exist_ok=True)
    path=cache/row['sha256'];proof=path.with_suffix('.json');lock=(cache/(row['sha256']+'.lock')).open('a+')
    fcntl.flock(lock,fcntl.LOCK_EX)
    try:
        if path.is_symlink() or proof.is_symlink():raise ValueError('Shared input cache cannot follow symbolic links')
        if path.exists():
            wanted={'sha256':row['sha256'],'bytes':row['bytes'],'identity':file_identity(path)}
            try:valid=json.loads(proof.read_text())==wanted
            except (OSError,ValueError):valid=False
            if not valid:
                h=hashlib.sha256()
                with path.open('rb') as f:
                    for data in iter(lambda:f.read(BUFFER),b''):h.update(data)
                valid=path.stat().st_size==row['bytes'] and h.hexdigest()==row['sha256']
                if valid:atomic_json(proof,wanted)
                else:path.unlink();proof.unlink(missing_ok=True)
            if valid:
                fcntl.flock(lock,fcntl.LOCK_SH);return CachedReader(path,lock)
        occupied=sum(p.stat().st_size for p in cache.iterdir() if is_sha(p.name) and p.is_file())
        for candidate in sorted((p for p in cache.iterdir() if is_sha(p.name) and p.is_file()),key=lambda p:p.stat().st_atime_ns):
            if occupied+row['bytes']<=budget:break
            guard=(cache/(candidate.name+'.lock')).open('a+')
            try:
                try:fcntl.flock(guard,fcntl.LOCK_EX|fcntl.LOCK_NB)
                except BlockingIOError:continue
                occupied-=candidate.stat().st_size;candidate.unlink();candidate.with_suffix('.json').unlink(missing_ok=True)
            finally:guard.close()
        if row['bytes']>budget or occupied+row['bytes']>budget or shutil.disk_usage(cache).free<row['bytes']:
            lock.close();return create()  # Caching is a preference, never a data-size cap.
        for stale in cache.glob(row['sha256']+'.*.partial'):stale.unlink(missing_ok=True)
        temporary=cache/(row['sha256']+'.'+secrets.token_hex(6)+'.partial');h=hashlib.sha256();count=0
        try:
            with create() as source,temporary.open('xb') as output:
                while data:=source.read(BUFFER):h.update(data);output.write(data);count+=len(data)
                output.flush();os.fsync(output.fileno())
                transferred=getattr(source,'transferred',count)
            if count!=row['bytes'] or h.hexdigest()!=row['sha256']:raise ValueError('Shared input checksum differs')
            temporary.chmod(0o600);os.replace(temporary,path)
            atomic_json(proof,{'sha256':row['sha256'],'bytes':row['bytes'],'identity':file_identity(path)})
        finally:temporary.unlink(missing_ok=True)
        fcntl.flock(lock,fcntl.LOCK_SH);return CachedReader(path,lock,transferred)
    except BaseException:
        lock.close();raise


class StreamInputs:
    """Common streaming interface for installed scientific recipe kernels."""
    def __init__(self,job,tls='/tls'):
        self.job=job;self.tls=Path(tls);self.config=json.loads((self.tls/'config.json').read_text())
        if self.config['machine']!=job['worker_machine']:raise ValueError('Streaming worker machine identity changed')
    def open(self,name):
        row=self.job['request']['inputs'][name];peer=self.config['peers'].get(row['peer'])
        if not peer or peer['machine']!=row['machine']:raise ValueError('Immutable input peer identity changed')
        create=lambda:RangeReader(row,peer,self.tls,self.config['port'],self.job['request']['buffer_bytes'])
        raw=shared_input(row,create,self.job.get('cache_directory','/cache'),self.job.get('cache_budget_bytes',0)) if row.get('mode')=='shared' else create()
        if isinstance(raw,CachedReader):raw.buffer=self.job['request']['buffer_bytes']
        return InputWindow(raw,row.get('start',0),row.get('length',row['bytes']-row.get('start',0)))



def publish_outputs(job,files,tls='/tls',attempts=3):
    """Stream immutable result files directly to an enrolled output grant.

    Only bounded blocks and compact receipts cross the node control journal.
    A lost acknowledgement is retried against the same immutable destination.
    """
    output=job['request']['output'];root=Path(tls)
    config=json.loads((root/'config.json').read_text());peer=config['peers'].get(output['peer'])
    if config['machine']!=job['worker_machine'] or not peer or peer['machine']!=output['machine']:raise ValueError('Output peer identity changed')
    context=ssl.create_default_context(cafile=str(root/'ca.crt'));context.minimum_version=ssl.TLSVersion.TLSv1_3
    context.load_cert_chain(str(root/'identity.crt'),str(root/'identity.key'));context.check_hostname=False
    receipts={}
    for name,path in files.items():
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,79}',name):raise ValueError('Invalid output filename')
        path=Path(path)
        if path.is_symlink() or not path.is_file():raise ValueError('Output must be a regular file')
        before=path.stat();identity=(before.st_dev,before.st_ino,before.st_size,before.st_mtime_ns)
        with path.open('rb') as stream:checksum=hashlib.file_digest(stream,'sha256').hexdigest()
        for attempt in range(attempts):
            connection=http.client.HTTPSConnection(peer['address'],peer.get('output_port',config['port']),context=context,timeout=60)
            try:
                connection.connect()
                if hashlib.sha256(connection.sock.getpeercert(binary_form=True)).hexdigest()!=peer['certificate_sha256']:raise ValueError('Output peer certificate differs')
                connection.putrequest('PUT','/inboxes/'+output['grant']+'/'+name)
                for key,value in {'Content-Length':str(before.st_size),'X-Content-SHA256':checksum,'X-Job-Key':output['job_key']}.items():connection.putheader(key,value)
                connection.endheaders()
                with path.open('rb') as stream:
                    while block:=stream.read(1024*1024):connection.send(block)
                response=connection.getresponse();response.read(65536)
                if response.status in (403,409,413,422):raise ValueError('Output grant or immutable contents rejected: '+str(response.status))
                if response.status not in (200,201) or response.getheader('ETag')!='"'+checksum+'"':raise OSError('Output publication not acknowledged: '+str(response.status))
                after=path.stat()
                if identity!=(after.st_dev,after.st_ino,after.st_size,after.st_mtime_ns):raise ValueError('Output changed during publication')
                receipts[name]={'bytes':before.st_size,'sha256':checksum};break
            except (OSError,http.client.HTTPException):
                if attempt+1==attempts:raise
                time.sleep(min(5,attempt+1))
            finally:connection.close()
    return receipts

def finish(job,outputs):
    """Installed recipe kernels commit compact output receipts through this contract."""
    atomic_json('/work/result.json',{'schema':VERSION,'contract_sha256':job['contract_sha256'],'recipe_fingerprint':job['recipe_fingerprint'],'worker_machine':job['worker_machine'],'outputs':outputs,'finished':time.time()})


def execute_builtin(job,tls='/tls',output='/work/result.json'):
    request=job['request'];inputs=StreamInputs(job,tls);expected=request['parameters'];compressed=hashlib.sha256();content=hashlib.sha256();compressed_count=0;count=0;pieces=0;last=0
    with inputs.open('data') as raw:
        class Hashed:
            def read(self,n=-1):
                nonlocal compressed_count
                data=raw.read(n);compressed.update(data);compressed_count+=len(data);return data
        with contextlib.ExitStack() as stack:
            decoded=stack.enter_context(gzip.GzipFile(fileobj=Hashed(),mode='rb')) if request['recipe']=='gzip.sha256.v1' else Hashed()
            while data:=decoded.read(request['buffer_bytes']):
                content.update(data);count+=len(data);pieces+=1
                if 'expected_bytes' in expected and count>expected['expected_bytes']:raise ValueError('Content exceeds assigned interval')
                if time.monotonic()-last>=2:
                    atomic_json(Path(output).with_name('progress.json'),{'decoded_bytes':count,'input_bytes':compressed_count,'pieces':pieces,'updated':time.time()});last=time.monotonic()
        row=request['inputs']['data'];length=row.get('length',row['bytes']-row.get('start',0))
        whole=row.get('start',0)==0 and length==row['bytes']
        if compressed_count!=length or whole and compressed.hexdigest()!=row['sha256']:raise ValueError('Immutable input checksum or byte count differs')
        if 'expected_bytes' in expected and count!=expected['expected_bytes']:raise ValueError('Content byte count differs from expected')
        if 'expected_sha256' in expected and content.hexdigest()!=expected['expected_sha256']:raise ValueError('Content digest differs from expected')
        result={'schema':VERSION,'contract_sha256':job['contract_sha256'],'recipe_fingerprint':job['recipe_fingerprint'],'worker_machine':job['worker_machine'],
                'outputs':{'sha256':content.hexdigest(),'bytes':count,'input_sha256':compressed.hexdigest(),'input_bytes':compressed_count,'pieces':pieces,'network_bytes':raw.raw.transferred,
                           'whole_object_verified':whole,'expected_content_verified':'expected_sha256' in expected},'finished':time.time()}
        atomic_json(output,result);return result


def validate_result(row,result,machine):
    spec=row['spec']
    if not isinstance(result,dict) or result.get('schema')!=VERSION or result.get('contract_sha256')!=row['fingerprint'] or result.get('recipe_fingerprint')!=spec['recipe_fingerprint'] or result.get('worker_machine')!=machine:raise ValueError('Streaming result contract identity differs')
    if spec['recipe'] not in BUILTINS:return
    outputs=result.get('outputs',{});source=spec['inputs']['data'];expected=spec['parameters']
    length=source.get('length',source['bytes']-source.get('start',0));whole=source.get('start',0)==0 and length==source['bytes']
    if not isinstance(outputs,dict) or not is_sha(outputs.get('sha256')) or not is_sha(outputs.get('input_sha256')) or type(outputs.get('bytes')) is not int or outputs['bytes']<0 or outputs.get('input_bytes')!=length or outputs.get('whole_object_verified') is not whole:raise ValueError('Streaming receipt has invalid byte or digest evidence')
    if whole and outputs['input_sha256']!=source['sha256']:raise ValueError('Streaming receipt input checksum differs')
    if spec['recipe']=='bytes.sha256.v1' and (outputs['bytes']!=length or outputs['sha256']!=outputs['input_sha256']):raise ValueError('Raw byte interval result differs')
    if any(outputs.get(key)!=wanted for key,wanted in [('bytes',expected.get('expected_bytes',outputs['bytes'])),('sha256',expected.get('expected_sha256',outputs['sha256']))]):raise ValueError('Streaming receipt differs from expected content')
    if outputs.get('expected_content_verified') is not ('expected_sha256' in expected) or type(outputs.get('pieces')) is not int or outputs['pieces']<int(outputs['bytes']>0):raise ValueError('Streaming receipt lacks verification evidence')


class Journal:
    """SQLite commits are the piece-level checkpoint and idempotent acknowledgement."""
    def __init__(self,path):
        self.path=Path(path);self.path.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
        with self.connect() as db:
            db.execute('PRAGMA journal_mode=WAL')
            db.execute('CREATE TABLE IF NOT EXISTS transitions(sequence INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT NOT NULL, state TEXT NOT NULL, attempt INTEGER NOT NULL, time REAL NOT NULL)')
            db.execute('CREATE INDEX IF NOT EXISTS transitions_timing ON transitions(id,attempt,state,time)')
            db.execute('CREATE TABLE IF NOT EXISTS work(id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, spec TEXT NOT NULL, state TEXT NOT NULL, attempt INTEGER NOT NULL DEFAULT 0, container TEXT, folder TEXT, due REAL NOT NULL DEFAULT 0, result TEXT, error TEXT, updated REAL NOT NULL)')
            columns={r[1] for r in db.execute('PRAGMA table_info(work)')}
            if 'priority' not in columns:db.execute('ALTER TABLE work ADD COLUMN priority INTEGER NOT NULL DEFAULT 0')
            if 'created' not in columns:
                db.execute('ALTER TABLE work ADD COLUMN created REAL NOT NULL DEFAULT 0')
                db.execute('UPDATE work SET created=updated')
        self.path.chmod(0o600)
    @contextlib.contextmanager
    def connect(self):
        db=sqlite3.connect(self.path,timeout=30);db.row_factory=sqlite3.Row;db.execute('PRAGMA synchronous=FULL')
        try:
            with db:yield db
        finally:db.close()
    def get(self,ident):
        with self.connect() as db:row=db.execute('SELECT * FROM work WHERE id=?',(ident,)).fetchone()
        if row is None:raise FileNotFoundError('Unknown streaming work item')
        value=dict(row);value['spec']=json.loads(value['spec'])
        if value['result']:value['result']=json.loads(value['result'])
        return value
    def submit(self,spec,allowed):
        fingerprint=digest(spec)
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE');old=db.execute('SELECT fingerprint FROM work WHERE id=?',(spec['id'],)).fetchone()
            if old and old['fingerprint']!=fingerprint:raise ValueError('Stream work ID already has a different immutable contract')
            if not old:
                if not allowed:raise PermissionError('Worker paused; new streaming tasks refused')
                db.execute('INSERT INTO work(id,fingerprint,spec,state,updated,priority,created) VALUES(?,?,?,?,?,?,?)',(spec['id'],fingerprint,canonical(spec),'queued',time.time(),spec.get('priority',0),time.time()))
                db.execute('INSERT INTO transitions(id,state,attempt,time) VALUES(?,?,?,?)',(spec['id'],'queued',0,time.time()))
        return self.get(spec['id'])
    def update(self,ident,**values):
        allowed={'state','attempt','container','folder','due','result','error','priority'}
        if set(values)-allowed:raise ValueError('Invalid work state update')
        values={**values,'updated':time.time()}
        if 'result' in values and values['result'] is not None:values['result']=canonical(values['result'])
        with self.connect() as db:
            db.execute('UPDATE work SET '+','.join(k+'=?' for k in values)+' WHERE id=?',(*values.values(),ident))
            if 'state' in values:
                row=db.execute('SELECT attempt FROM work WHERE id=?',(ident,)).fetchone()
                db.execute('INSERT INTO transitions(id,state,attempt,time) VALUES(?,?,?,?)',(ident,values['state'],row['attempt'],time.time()))
    def timings(self,ids):
        import datetime
        now=time.time();out={}
        if not ids:return out
        with self.connect() as db:
            rows=db.execute('SELECT w.id,w.state,w.attempt,w.spec,w.container,w.updated,w.result,MIN(t.time) AS started '
                'FROM work w LEFT JOIN transitions t ON t.id=w.id AND t.attempt=w.attempt AND t.state=\'running\' '
                'WHERE w.id IN ('+','.join('?' for _ in ids)+') GROUP BY w.id',ids).fetchall()
            for row in rows:
                end=None
                if row['state'] not in ('running','launching','admitting','queued'):
                    terminal=db.execute('SELECT MAX(time) FROM transitions WHERE id=? AND attempt=? AND state=?',
                        (row['id'],row['attempt'],row['state'])).fetchone()[0]
                    end=terminal
                start=row['started']
                if row['state']=='completed' and start is not None and end is not None:
                    receipt=json.loads(row['result'] or '{}');finished=receipt.get('finished') if isinstance(receipt,dict) else None
                    if type(finished) in (int,float) and start<=finished<=end:end=finished
                elapsed=(end if end is not None else now)-start if start is not None else None
                iso=lambda value:datetime.datetime.fromtimestamp(value,datetime.timezone.utc).isoformat() if value is not None else None
                spec=json.loads(row['spec'])
                out[row['id']]={'attempt':row['attempt'],'state':row['state'],'started':iso(start),'finished':iso(end),
                    'elapsed_seconds':elapsed if elapsed is not None and elapsed>=0 else None,
                    'container':row['container'],'resources':spec.get('resources'),'image':spec.get('image'),
                    'recorded_at':row['updated'],'sampled_at':now,
                    'timing_basis':'node running transition to verified worker finish (terminal transition fallback); includes transfer and GPU waits; excludes prior attempts'}
        return out

    def list(self,states=None,limit=100,after='',has_container=False):
        with self.connect() as db:
            rows=db.execute('SELECT id FROM work WHERE id>? '+('AND state IN ('+','.join('?' for _ in states)+') ' if states else '')+('AND container IS NOT NULL ' if has_container else '')+'ORDER BY id LIMIT ?',(after,*(states or ()),limit)).fetchall()
        return [self.get(row['id']) for row in rows]


class Engine:
    """Node-local admission and recovery; Docker workers survive launcher reloads."""
    def __init__(self,agent,lease_factory,policy,start=True,state_root=None):
        self.agent=agent;self.lease_factory=lease_factory;self.policy=policy;self.root=Path(state_root or agent.root)/'.stream-work';self.root.mkdir(mode=0o700,exist_ok=True)
        self.store=Journal(self.root/'work.sqlite3');self.stopped=threading.Event();self.pending={};self.lock=threading.RLock();self.last_error=None
        self.machine=hashlib.sha256(Path('/etc/machine-id').read_bytes()).hexdigest();self.registry=recipes(agent.config)
        self.runtime=Path(__file__).resolve();self.revision=hashlib.sha256(self.runtime.read_bytes()).hexdigest()
        if start:
            self.thread=threading.Thread(target=self.loop,name='launcher-stream-work',daemon=True);self.thread.start()
    def definition(self,spec):
        if spec.get('recipe_definition') is not None:return spec['recipe_definition']
        if spec['recipe'] in BUILTINS:return {'image':spec['image'],'argv':['python3','/runtime/launcher_streams.py','--worker','/work/job.json'],'version':spec.get('runtime_sha256',self.revision)}
        return self.registry[spec['recipe']]
    def reply(self,row):
        result={k:row[k] for k in ('id','state','attempt','updated','result','error','container','priority')}
        result['contract_sha256']=row['fingerprint']
        result['execution']=self.store.timings([row['id']]).get(row['id'])
        if row.get('folder'):
            progress=Path(row['folder'])/'progress.json'
            try:result['progress']=json.loads(progress.read_text())
            except (OSError,ValueError):pass
        return result
    def dispatch(self,op,args):
        # Progress reads must not wait behind Docker admission or image probes.
        # The journal owns its SQLite transaction locks independently.
        if op=='stream.status':
            if set(args)!={'id'} or not isinstance(args['id'],str):raise ValueError('Expected a streaming work ID')
            return self.reply(self.store.get(args['id']))
        if op=='stream.timings':
            ids=args.get('ids')
            if set(args)!={'ids'} or not isinstance(ids,list) or not 1<=len(ids)<=100 or any(not isinstance(i,str) or len(i)>200 for i in ids):raise ValueError('Expected up to 100 streaming IDs')
            return {'items':self.store.timings(ids)}
        if op=='stream.list':
            if set(args)-{'limit','after'} or type(args.get('limit',100)) is not int or not 1<=args.get('limit',100)<=100:raise ValueError('Invalid streaming page')
            rows=self.store.list(limit=args.get('limit',100),after=args.get('after',''))
            return {'items':[self.reply(row) for row in rows],'next':rows[-1]['id'] if len(rows)==args.get('limit',100) else None}
        if op=='stream.capabilities':self.registry=recipes(self.agent.config)
        if op=='stream.capabilities':
            if args:raise ValueError('Streaming capabilities accepts no arguments')
            return {'version':VERSION,'runtime_sha256':self.revision,'recipes':list(BUILTINS)+sorted(self.registry),'buffer_bytes':BUFFER,'durable_checkpoints':True,'queue_priorities':True,'gpu_recipes':True,'shared_reference_volumes':True,'direct_result_export':True,'payload_cache':'streamed-by-default; shared verified references opt-in','shared_resource_pool':True,'storage_modes':['durable','ram-only'],'concurrency':'resource-admitted; no fixed job-count cap','last_error':self.last_error}
        with self.lock:
            if op=='stream.submit':self.registry=recipes(self.agent.config)
            if op=='stream.submit':
                # Acknowledging already accepted work must use its pinned
                # recipe, even after an owner installs a different image.
                old=None
                if isinstance(args,dict) and isinstance(args.get('id'),str):
                    try:old=self.store.get(args['id'])
                    except FileNotFoundError:pass
                registry=self.registry
                if old and old['spec'].get('recipe_definition') is not None and old['spec']['recipe'] not in BUILTINS:
                    registry={**registry,old['spec']['recipe']:old['spec']['recipe_definition']}
                spec=validate(args,registry)
                if old:
                    prior={k:v for k,v in old['spec'].items() if k not in ('recipe_fingerprint','runtime_sha256','recipe_definition')}
                    if prior!=spec:raise ValueError('Stream work ID already has a different immutable contract')
                    return self.reply(old)
                definition=self.definition(spec);spec={**spec,'recipe_fingerprint':digest(definition),'runtime_sha256':self.revision,'recipe_definition':definition}
                for row in [*spec['inputs'].values(),*([spec['output']] if spec.get('output') else [])]:
                    config=self.bulk()['config']
                    peer=config['peers'].get(row['peer'])
                    if not peer or peer['machine']!=row['machine']:raise ValueError('Streaming input is not an enrolled bulk peer')
                row=self.store.submit(spec,self.policy(self.agent.config)['accepting_jobs']);return self.reply(row)
            if op=='stream.priority':
                if set(args)!={'id','priority'} or not isinstance(args['id'],str) or type(args['priority']) is not int or not -1000<=args['priority']<=1000:raise ValueError('Expected ID and integer priority from -1000 to 1000')
                row=self.store.get(args['id'])
                lease=self.pending.get(row['id'])
                if lease:
                    with lease.guard():lease.row['priority']=args['priority'];lease.write()
                self.store.update(row['id'],priority=args['priority'],**({'due':0} if row['state'] in ('queued','retry_wait') else {}))
                return self.reply(self.store.get(row['id']))
            if set(args)!={'id'} or not isinstance(args['id'],str):raise ValueError('Expected a streaming work ID')
            row=self.store.get(args['id'])
            if op=='stream.retry':
                if row['state'] in ('retry_wait','failed','cancelled'):
                    if not self.policy(self.agent.config)['accepting_jobs']:raise PermissionError('Worker paused')
                    self.store.update(row['id'],state='queued',due=0,error=None)
                elif row['state']=='failed_integrity':raise ValueError('Integrity failures require corrected inputs and a new work contract')
                return self.reply(self.store.get(row['id']))
            if op=='stream.cancel':
                if row['state'] not in TERMINAL:
                    if row['container']:
                        state=self.inspect(row)
                        if state and state.get('Running'):self.command(['docker','stop','--time','30',row['container']])
                    self.store.update(row['id'],state='cancelled',error='Owner cancelled streaming work')
                return self.reply(self.store.get(row['id']))
            raise ValueError('Unsupported streaming work operation')
    def bulk(self):
        path=self.agent.config.parent/'bulk-streams.json'
        if path.exists():value=json.loads(path.read_text())
        else:
            base=Path.home()/'.local/state/lifetech-launcher/bulk-transport'
            value={'root':str(base),'config_dir':str(self.agent.config.parent),'config':json.loads((base/'config.json').read_text())}
        if value['config']['machine']!=self.machine or value['config']['name']!=json.loads(self.agent.config.read_text())['name'] or not Path(value['root']).is_absolute():raise ValueError('Enrolled streaming transport identity differs')
        return value
    def cache_budget(self):
        import shutil
        free=shutil.disk_usage(self.root).free
        config=json.loads(self.agent.config.read_text())
        budget=config.get('stream_cache_bytes',free//4)
        return max(0,min(budget,free//2)) if type(budget) is int else 0
    def host_path(self,path):
        path=Path(path).resolve()
        if not Path('/.dockerenv').exists():return str(path)
        config_dir=self.bulk()['config_dir'];matches=[]
        # Standard run.py owner bootstrap can attest identity-mapped mounts
        # even when Docker lost live-restore metadata for the launcher itself.
        # These are owner-installed routing records, never job parameters.
        for mapping in self.bulk().get('host_paths',[]):
            target=Path(mapping['container']);source=Path(mapping['host'])
            if not target.is_absolute() or not source.is_absolute() or '..' in target.parts or '..' in source.parts:
                raise ValueError('Invalid enrolled host path mapping')
            if path.is_relative_to(target):matches.append((len(target.parts),str(source/path.relative_to(target))))
        if matches:return max(matches)[1]
        with self.agent.client() as client:
            for name in ('organization-test-agent','swarm-node','lifetech-launcher','lifetech-launcher-session'):
                try:container=client.containers.get(name)
                except Exception:continue
                if not container.attrs.get('State',{}).get('Running'):continue
                mounts=container.attrs.get('Mounts',[])
                if not any(m['Destination']==str(self.agent.config.parent) and m['Source']==config_dir for m in mounts):continue
                for mount in mounts:
                    destination=Path(mount['Destination'])
                    if mount.get('Type')=='bind' and path.is_relative_to(destination):matches.append((len(destination.parts),str(Path(mount['Source'])/path.relative_to(destination))))
        if not matches:raise ValueError('Streaming worker path has no enrolled host bind mount')
        return max(matches)[1]
    def worker_user(self):
        if not hasattr(self,'_worker_user'):
            with self.agent.client() as client:options=client.info().get('SecurityOptions',[])
            # Root in a rootless daemon maps to the enrolled host owner, not
            # host root. UID 1000 there maps to a subordinate account and cannot
            # read the owner's private runtime, TLS keys or control journal.
            rootless=any(option=='name=rootless' for option in options)
            self._worker_user='0:0' if rootless else str(os.getuid())+':'+str(os.getgid())
        return self._worker_user
    def command(self,args,**kwargs):return subprocess.run(args,check=True,capture_output=True,text=True,timeout=30,**kwargs)
    def inspect(self,row):
        result=subprocess.run(['docker','inspect',row['container']],capture_output=True,text=True,timeout=15)
        if result.returncode:
            if 'no such' in result.stderr.lower():return None
            raise RuntimeError('Cannot determine streaming container state')
        value=json.loads(result.stdout)[0];labels=value['Config'].get('Labels') or {}
        if labels.get('org.lifetech.stream-work')!=row['id'] or labels.get('org.lifetech.stream-contract')!=row['fingerprint'] or value['Image']!=row['spec']['image']:raise ValueError('Streaming worker container identity differs')
        attempt=labels.get('org.lifetech.stream-attempt')
        if not isinstance(attempt,str) or not attempt.isdecimal() or int(attempt)<1:raise ValueError('Streaming attempt identity missing')
        folder=str(self.root/'work'/row['id']/attempt)
        if row['folder']!=folder:
            self.store.update(row['id'],folder=folder);row['folder']=folder
        return value['State']
    def resource(self,row):
        spec=row['spec'];r=spec['resources'];return self.lease_factory('stream-work',r['cpus'],r['memory_mb'],row['container'],directory=os.getenv('SWARM_RESOURCE_DIR',str(self.agent.root/'.resources')),pids=max(32,r['cpus']*4+16),priority=row.get('priority',0),sink='gpu' if self.definition(spec).get('gpu') else 'cpu')
    def execution_bindings(self,definition):
        """Only owner-installed bindings can expose devices or shared volumes."""
        validate_execution(definition)
        args=[];gpu=definition.get('gpu');cache=definition.get('shared_cache')
        if cache:
            owner=json.loads(self.command(['docker','inspect',cache['container']]).stdout)[0]
            volume=json.loads(self.command(['docker','volume','inspect',cache['volume']]).stdout)[0]
            options=volume.get('Options') or {}
            if (owner.get('Image')!=cache['image'] or (owner['Config'].get('Labels') or {}).get('org.lifetech.cache-owner')!=cache['key']
                    or volume.get('Driver')!='local' or options.get('type')!='tmpfs' or options.get('device')!='tmpfs'
                    or (volume.get('Labels') or {}).get('org.lifetech.cache-owner')!=cache['key']
                    or 'size='+str(cache['shm_mb'])+'m' not in options.get('o','').split(',')):raise ValueError('Shared reference owner/volume identity differs')
            if not owner['State'].get('Running'):raise RuntimeError('Shared reference owner unavailable')
            code="import json,pathlib; p=pathlib.Path('/dev/shm'); r=json.loads((p/'.ready.json').read_text()); print(json.dumps({'proof':r,'identities':{n:[s.st_dev,s.st_ino,s.st_size,s.st_mtime_ns] for n in r['files'] for s in [(p/n).stat()]}}))"
            ready=json.loads(self.command(['docker','exec',cache['container'],'python3','-c',code]).stdout)
            proof=ready['proof']
            if (any(proof.get(k)!=cache[k] for k in ('key','files')) or proof.get('container_id')!=owner['Id']
                    or proof.get('started_at')!=owner['State']['StartedAt'] or ready['identities']!=proof.get('identities')):raise ValueError('Shared reference readiness proof differs')
            args+=['--mount','type=volume,src='+cache['volume']+',dst=/dev/shm,readonly,volume-nocopy','--label','org.lifetech.shared-cache='+cache['container']]
        if gpu:
            active=self.command(['docker','ps','-q','--filter','label=org.lifetech.accelerator='+gpu['uuid']]).stdout.strip()
            if active and gpu.get('max_concurrency',1)==1:raise RuntimeError('Physical GPU already has an admitted worker')
            if gpu.get('render_device'):
                device=Path('/sys/class/drm')/Path(gpu['render_device']).name/'device'
                free={gpu['uuid']:(int((device/'mem_info_vram_total').read_text())-int((device/'mem_info_vram_used').read_text()))//1024**2}
            else:
                rows=self.command(['nvidia-smi','--query-gpu=uuid,memory.free','--format=csv,noheader,nounits']).stdout.splitlines()
                free={line.split(',')[0].strip():int(line.split(',')[1]) for line in rows}
            if free.get(gpu['uuid'],0)<gpu['minimum_free_mib']:raise RuntimeError('Physical GPU headroom unavailable')
            # This is a host-daemon bind path installed by the node owner;
            # it need not exist in the launcher's own mount namespace.
            directory=Path(gpu['lock_directory'])
            args+=gpu_container_args(gpu)+[
                   '--mount','type=bind,src='+str(directory)+',dst=/gpu-locks','--label','org.lifetech.accelerator='+gpu['uuid'],'--label','org.lifetech.sink=gpu','--label','org.lifetech.gpu-concurrency='+str(gpu.get('max_concurrency',1)),'--label','org.lifetech.gpu-private-mib='+str(gpu.get('vram_mb',gpu['minimum_free_mib']))]
        return args

    def launch(self,row):
        definition=self.definition(row['spec'])
        if digest(definition)!=row['spec']['recipe_fingerprint']:raise ValueError('Installed recipe changed; submit a new work contract')
        if row['state'] in ('queued','retry_wait'):
            if row['container']:
                state=self.inspect(row)
                if state and state.get('Running'):
                    self.store.update(row['id'],state='running');return
                if state:self.command(['docker','rm',row['container']])
                old=self.pending.pop(row['id'],None)
                if old:old.close()
            attempt=row['attempt']+1;name='swarm-job-'+digest({'id':row['id'],'contract':row['fingerprint']})[:32];folder=self.root/'work'/row['id']/str(attempt);folder.mkdir(mode=0o700,parents=True,exist_ok=True)
            self.store.update(row['id'],state='admitting',attempt=attempt,container=name,folder=str(folder),error=None);row=self.store.get(row['id'])
        # Reattach before reserving resources: a late acknowledged worker may
        # already consume the node's entire budget after the agent reloads.
        state=self.inspect(row)
        if state:
            self.store.update(row['id'],state='launching')
            self.reconcile(self.store.get(row['id']));return
        if definition.get('gpu') and hasattr(self.agent,'gpu_resources'):
            definition={**definition,'gpu':resolve_gpu_allocation(definition['gpu'],self.agent.gpu_resources(),self.machine)}
        bindings=self.execution_bindings(definition)
        lease=self.pending.get(row['id'])
        if lease is None:lease=self.resource(row);self.pending[row['id']]=lease
        if not lease.try_acquire():return
        if not self.policy(self.agent.config)['accepting_jobs']:
            lease.close();self.pending.pop(row['id'],None);self.store.update(row['id'],state='queued');return
        spec=row['spec'];folder=Path(row['folder']);bulk=self.bulk();tls=Path(bulk['root'])
        runtime=self.runtime if spec['runtime_sha256']==self.revision else self.agent.root/'.stream-runtime'/spec['runtime_sha256']/'launcher_streams.py'
        if not runtime.is_file() or hashlib.sha256(runtime.read_bytes()).hexdigest()!=spec['runtime_sha256']:raise ValueError('Pinned streaming runtime is unavailable')
        job={'request':spec,'contract_sha256':row['fingerprint'],'recipe_fingerprint':spec['recipe_fingerprint'],'worker_machine':self.machine,'cache_budget_bytes':self.cache_budget()}
        ram=spec.get('storage',{}).get('mode')=='ram-only'
        if ram:job.update(ram_argv=definition['argv'],cache_budget_bytes=0)
        if definition.get('gpu'):
            job.update(ram_argv=definition['argv'],gpu_lock='/gpu-locks/'+definition['gpu']['uuid']+'.lock')
            if definition['gpu'].get('max_concurrency',1)>1 or definition['gpu'].get('render_device'):job['gpu_admission']={**definition['gpu'],'lock_directory':'/gpu-locks'}
        if definition.get('shared_cache'):job['shared_reference']={k:definition['shared_cache'][k] for k in ('key','memory_mb','container')}
        atomic_json(folder/'job.json',job)
        args=['docker','run','-d','--pull=never','--name',row['container'],'--network','host','--read-only','--log-driver','none','--cpus',str(spec['resources']['cpus']),'--memory',str(spec['resources']['memory_mb'])+'m','--memory-swap',str(spec['resources']['memory_mb'])+'m','--pids-limit',str(max(32,spec['resources']['cpus']*4+16)),'--cap-drop','ALL','--security-opt','no-new-privileges','--user',self.worker_user(),'--tmpfs','/tmp:rw,nosuid,nodev,noexec,size='+str(max(1,spec['resources']['memory_mb']//8))+'m',
              '--label','org.lifetech.worker=true','--label','org.lifetech.job='+row['container'].removeprefix('swarm-job-'),'--label','org.lifetech.group=stream-work','--label','org.lifetech.stream-work='+row['id'],'--label','org.lifetech.stream-contract='+row['fingerprint'],'--label','org.lifetech.stream-attempt='+str(row['attempt']),'-e','PYTHONPATH=/runtime','-w','/work','--mount','type=bind,src='+self.host_path(folder)+',dst=/work','--mount','type=bind,src='+self.host_path(runtime)+',dst=/runtime/launcher_streams.py,readonly']
        if ram:
            # Keep the compact control journal separate from all payload and
            # scratch data. Container tmpfs is charged to the same no-swap lease.
            work_mount='type=bind,src='+self.host_path(folder)+',dst=/work'
            args[args.index(work_mount)]='type=bind,src='+self.host_path(folder)+',dst=/control'
            args+=['--tmpfs','/work:rw,nosuid,nodev,noexec,size='+str(spec['storage']['scratch_mb'])+'m,mode=1777',
                   '--tmpfs','/cache:rw,nosuid,nodev,noexec,size=1m,mode=1777',
                   '--mount','type=bind,src='+self.host_path(folder/'job.json')+',dst=/work/job.json,readonly']
        else:
            cache=self.root/'cache';cache.mkdir(mode=0o700,exist_ok=True)
            args+=['--mount','type=bind,src='+self.host_path(cache)+',dst=/cache']
        for name in ('config.json','ca.crt','identity.crt','identity.key'):args+=['--mount','type=bind,src='+str(tls/name)+',dst=/tls/'+name+',readonly']
        args+=bindings
        for mount in definition.get('mounts',[]):args+=['--mount','type=bind,src='+mount['source']+',dst='+mount['target']+(',readonly' if mount['readonly'] else '')]
        args+=(['--entrypoint','python3',definition['image'],'/runtime/launcher_streams.py','--ram-worker','/work/job.json'] if ram else ['--entrypoint','python3',definition['image'],'/runtime/launcher_streams.py','--recipe-worker','/work/job.json'] if definition.get('gpu') else ['--entrypoint',definition['argv'][0],definition['image'],*definition['argv'][1:]])
        self.store.update(row['id'],state='launching')
        try:self.command(args)
        except (OSError,subprocess.SubprocessError):
            # An acknowledged Docker start may outlive a lost CLI response.
            state=self.inspect(row)
            if state is None:raise
        self.store.update(row['id'],state='running')
    def reconcile(self,row):
        state=self.inspect(row)
        if state and state.get('Running'):
            if row['state']!='running':self.store.update(row['id'],state='running')
            return
        if state and state.get('Status')=='created':
            if self.policy(self.agent.config)['accepting_jobs']:
                self.command(['docker','start',row['container']]);self.store.update(row['id'],state='running')
            return
        if state is None and row['state']=='launching' and time.time()-row['updated']<35:return
        path=Path(row['folder'])/'result.json'
        if state and state.get('ExitCode')==0 and path.exists():
            if path.stat().st_size>1024**2:raise ValueError('Recipe result must be a compact receipt; keep artifacts outside the control envelope')
            result=json.loads(path.read_text())
            validate_result(row,result,self.machine)
            self.store.update(row['id'],state='completed',result=result,error=None)
        elif state and state.get('ExitCode')==78:
            self.store.update(row['id'],state='failed_integrity',error='Input or recipe integrity validation failed; publication forbidden')
        elif state and state.get('ExitCode') not in (0,75,137,143) and not state.get('OOMKilled'):
            self.store.update(row['id'],state='failed',error='Installed recipe failed; explicit retry required')
        else:
            reason='Worker memory budget exhausted; increase declared resources in a new contract' if state and state.get('OOMKilled') else 'Worker or transport interrupted; piece checkpoint retained'
            self.store.update(row['id'],state='failed' if state and state.get('OOMKilled') else 'retry_wait',due=time.time()+min(60,2**min(row['attempt'],6)),error=reason)
        if state:self.command(['docker','rm',row['container']])
        self.store.update(row['id'],container=None)
        lease=self.pending.pop(row['id'],None)
        if lease:lease.close()
    def cleanup(self,row):
        state=self.inspect(row) if row['container'] else None
        if state:
            if state.get('Running'):return
            self.command(['docker','rm',row['container']])
        lease=self.pending.pop(row['id'],None)
        if lease:lease.close()
        self.store.update(row['id'],container=None)
    def tick(self):
        with self.lock:
            allowed=self.policy(self.agent.config)['accepting_jobs']
            for row in self.rows(states=('launching','running')):
                try:self.reconcile(row)
                except (ValueError,TypeError,KeyError) as error:
                    self.store.update(row['id'],state='failed_integrity',error=str(error));self.cleanup(row)
            for row in self.rows(states=TERMINAL,has_container=True):
                if row['container'] or row['id'] in self.pending:self.cleanup(row)
            for row in sorted(self.rows(states=('queued','retry_wait','admitting')),key=lambda r:(-r['priority'],r['created'],r['id'])):
                if not allowed:
                    lease=self.pending.pop(row['id'],None)
                    if lease:lease.close()
                    continue
                if row['due']>time.time():continue
                try:self.launch(row)
                except ValueError as error:
                    current=self.store.get(row['id']);self.store.update(row['id'],state='failed',error=str(error))
                    self.cleanup(current)
                except (OSError,RuntimeError,subprocess.SubprocessError):
                    current=self.store.get(row['id'])
                    if current['container'] and self.inspect(current):continue
                    lease=self.pending.pop(row['id'],None)
                    if lease:lease.close()
                    self.store.update(row['id'],state='retry_wait',due=time.time()+5,error='Worker admission or image unavailable')
    def rows(self,**filters):
        after=''
        while True:
            rows=self.store.list(after=after,**filters)
            if not rows:return
            yield from rows
            after=rows[-1]['id']
    def loop(self):
        while not self.stopped.is_set():
            try:self.tick();self.last_error=None
            except Exception as error:self.last_error=type(error).__name__+': '+str(error)[:160]
            self.stopped.wait(.5)
    def close(self):
        self.stopped.set()
        if hasattr(self,'thread'):self.thread.join(timeout=20)
        with self.lock:
            for lease in self.pending.values():lease.close()
            self.pending.clear()


def export_control_file(source,destination,limit):
    """Only compact JSON receipts/progress cross the RAM-to-control boundary."""
    source=Path(source)
    if source.is_symlink() or not source.is_file() or source.stat().st_size>limit:
        raise ValueError('Invalid RAM worker control receipt')
    with source.open('rb') as stream:data=stream.read(limit+1)
    if len(data)>limit:raise ValueError('RAM worker control receipt too large')
    value=json.loads(data)
    atomic_json(destination,value)


def execute_ram(job,control=True):
    argv=job['ram_argv']
    guard=None
    if job.get('gpu_admission'):
        namespace={'__name__':'gpu_admission'};exec(GPU_ADMISSION_SOURCE,namespace)
        try:guard=namespace['acquire_gpu'](job['gpu_admission'])
        except BlockingIOError:return 75
    elif job.get('gpu_lock'):
        guard=open(job['gpu_lock'],'a')
        try:fcntl.flock(guard,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:guard.close();return 75
    try:process=subprocess.Popen(argv)
    except BaseException:
        if guard:guard.close()
        raise
    try:
        while process.poll() is None:
            try:
                if control:export_control_file('/work/progress.json','/control/progress.json',65536)
            except (OSError,ValueError):pass
            time.sleep(.25)
        if process.returncode:return process.returncode
        if not control:return 0
        export_control_file('/work/result.json','/control/result.json',1024**2)
        try:export_control_file('/work/progress.json','/control/progress.json',65536)
        except FileNotFoundError:pass
        return 0
    finally:
        if process.poll() is None:process.terminate();process.wait()
        if guard:guard.close()


if __name__=='__main__':
    if len(sys.argv)==3 and sys.argv[1] in ('--ram-worker','--recipe-worker'):
        job=json.loads(Path(sys.argv[2]).read_text())
        try:raise SystemExit(execute_ram(job,control=sys.argv[1]=='--ram-worker'))
        except (ValueError,EOFError,gzip.BadGzipFile):raise SystemExit(78)
        except (OSError,http.client.HTTPException):raise SystemExit(75)
    elif len(sys.argv)==3 and sys.argv[1]=='--worker':
        job=json.loads(Path(sys.argv[2]).read_text())
        try:execute_builtin(job)
        except (ValueError,EOFError,gzip.BadGzipFile):raise SystemExit(78)
        except (OSError,http.client.HTTPException):raise SystemExit(75)
    else:raise SystemExit('Use launcher stream APIs or --worker <job.json>')
