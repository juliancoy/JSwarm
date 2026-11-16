"""Paired native CPU vs full shared GPU-service uint32 gather, not alignment."""
import array
import concurrent.futures as futures
import ctypes as C
import hashlib
import json
import multiprocessing as mp
import os
from pathlib import Path
import statistics
import subprocess
import sys
import tempfile
import time
import uuid

ROOT=Path(__file__).resolve().parents[3]
BASE=Path(__file__).resolve().parent
sys.path[:0]=[str(ROOT/'JSwarm'),str(ROOT/'JSwarm/scripts'),str(ROOT/'JSwarm/swarm-control/server')]
from swarm_gpu_memory import Session,request
from agent_client import AgentClient,CONFIG
from scripts.launcher_discovery import resolve_endpoint
WORDS=None
BARRIER=None


def worker(task):
    mode,pattern,path,digest,count,seed=task
    lib=C.CDLL(str(BASE/'cpu-gather.so'))
    lib.cpu_gather.argtypes=[C.POINTER(C.c_uint32),C.c_uint64,C.POINTER(C.c_uint32),C.POINTER(C.c_uint32),C.c_uint32]
    lib.cpu_gather.restype=C.c_int
    lib.cpu_indices.argtypes=[C.POINTER(C.c_uint32),C.c_uint32,C.c_uint32,C.c_uint32]
    lib.cpu_indices.restype=C.c_uint32
    reference=(C.c_uint32*len(WORDS)).from_buffer(WORDS)
    session=Session(path,digest) if mode=='gpu' else None
    raw=memoryview(bytearray(65536*8)) if session is None else None
    inputs=raw[:65536*4].cast('I') if raw is not None else session.inputs
    outputs=raw[65536*4:].cast('I') if raw is not None else session.outputs
    source=(C.c_uint32*65536).from_buffer(inputs)
    dest=(C.c_uint32*65536).from_buffer(outputs)
    state=seed
    def generate():
        nonlocal state
        state=lib.cpu_indices(source,count,len(WORDS),state)
    def gather():
        if session is not None:session.gather(count)
        else:assert lib.cpu_gather(reference,len(WORDS),source,dest,count)==1
    def verify():
        assert all(outputs[i]==WORDS[inputs[i]] for i in range(count))
    try:
        generate()
        for _ in range(3):gather();verify()
        # CPU invalid-index contract: reject before any result write.
        if session is None:
            saved=inputs[0];inputs[0]=len(WORDS)
            assert lib.cpu_gather(reference,len(WORDS),source,dest,count)==0
            inputs[0]=saved
        BARRIER.wait(timeout=30)
        samples=[]
        for iteration in range(100):
            if pattern=='changing':generate()
            else:inputs[0]=((seed+iteration)*7919)%len(WORDS)
            start=time.perf_counter();gather();samples.append(time.perf_counter()-start)
            assert outputs[0]==WORDS[inputs[0]] and outputs[count-1]==WORDS[inputs[count-1]]
        finished=time.perf_counter()
        verify()
        return samples,finished
    finally:
        del source,dest,reference
        if session is not None:session.close()
        else:inputs.release();outputs.release();raw.release()


def main():
    global WORDS,BARRIER
    info=subprocess.check_output(['lscpu'],text=True)
    result={'scope':'64 MiB synthetic uint32 gather, not alignment; CPU is compiled -O3 -march=native (not Python lookup); GPU includes shared-service IPC and completion; identical index streams and bounds validation; three trials of 100 batches per worker; no CPU affinity or GPU exclusivity',
            'timing':'throughput includes generating changing indices and first/last output checks; per-batch latency excludes generation/checks; full warmup/final validation and process/session setup/teardown excluded',
            'cpu':next(line.split(':',1)[1].strip() for line in info.splitlines() if line.startswith('Model name:')),
            'visible_cpu_count':os.cpu_count(),'cpu_affinity':sorted(os.sched_getaffinity(0)),
            'gpu':subprocess.check_output(['nvidia-smi','--query-gpu=name,driver_version,memory.used,memory.free,utilization.gpu','--format=csv,noheader'],text=True).strip(),
            'compiler':subprocess.check_output(['g++','--version'],text=True).splitlines()[0],
            'reference_bytes':64*1024**2,'rows':[]}
    config=json.loads(CONFIG.read_text());name=config['name'];endpoint,_=resolve_endpoint(name)
    agent=AgentClient({'host':name,'agent_endpoint':endpoint,'local':True});lease=None
    try:
        for cpus in (8,4):
            lease=uuid.uuid4().hex
            admission=agent.call('resource.acquire',{'id':lease,'group':'cpu-gpu-paired-comparison','cpus':cpus,'memory_mb':1024,'container':'swarm-job-'+lease,'sink':'gpu','priority':-100},timeout=15)
            if admission.get('acquired'):break
            agent.call('resource.release',{'id':lease},timeout=15);lease=None
        if lease is None:raise RuntimeError('No capacity for bounded paired comparison')
        result['admitted_cpu_budget']=cpus
        with tempfile.TemporaryDirectory(prefix='gmc-',dir='/tmp') as tmp:
            folder=Path(tmp);source=folder/'reference';ready=folder/'ready'
            WORDS=array.array('I',range(65536))*256
            source.write_bytes(WORDS.tobytes());source.chmod(0o400)
            digest=hashlib.sha256(source.read_bytes()).hexdigest();result['reference_sha256']=digest
            command=[sys.executable,str(ROOT/'JSwarm/scripts/swarm_gpu_memory.py'),'--source',str(source),'--sha256',digest,'--directory',str(folder/'owner'),'--ready',str(ready),'--budget',str(len(WORDS)*4),'--segment',str(16*1024**2),'--parallelism','4','--max-sessions','16','--spin-us','0']
            with (folder/'stderr').open('w+') as err:
                process=subprocess.Popen(command,stderr=err)
                try:
                    deadline=time.monotonic()+20
                    while not ready.exists() and process.poll() is None and time.monotonic()<deadline:time.sleep(.01)
                    if not ready.exists():err.seek(0);raise RuntimeError(err.read())
                    path=json.loads(ready.read_text())['socket']
                    for clients in (1,cpus):
                        for pattern in ('warm','changing'):
                            for count in (4096,65536):
                                # Alternate mode order across cases; sequential to
                                # avoid CPU/GPU cases competing with each other.
                                modes=('cpu','gpu') if (clients==1)==(pattern=='warm') else ('gpu','cpu')
                                for mode in modes:
                                    trials=[];samples=[]
                                    for trial in range(3):
                                        BARRIER=mp.get_context('fork').Barrier(clients+1)
                                        with futures.ProcessPoolExecutor(max_workers=clients,mp_context=mp.get_context('fork')) as pool:
                                            pending=[pool.submit(worker,(mode,pattern,path,digest,count,1+i+trial*clients)) for i in range(clients)]
                                            BARRIER.wait(timeout=30);start=time.perf_counter();finished=[]
                                            for job in pending:
                                                timings,end=job.result(timeout=30);samples.extend(timings);finished.append(end)
                                            trials.append(max(finished)-start)
                                    row={'mode':mode,'workers':clients,'pattern':pattern,'batch_words':count,'trial_seconds':trials,
                                         'lookups_per_s':clients*100*count/statistics.median(trials),
                                         'latency_p50_ms':statistics.median(samples)*1000,
                                         'latency_p95_ms':sorted(samples)[int((len(samples)-1)*.95)]*1000}
                                    result['rows'].append(row);print(json.dumps(row),flush=True)
                    gpu_info=request(path,digest)
                    assert gpu_info['reference_upload_bytes']==len(WORDS)*4
                    assert gpu_info['socket_payload_bytes']==0
                    assert gpu_info['query_upload_copy_bytes']==gpu_info['result_readback_copy_bytes']==0
                    result['owner']=gpu_info
                finally:
                    process.terminate()
                    try:process.wait(timeout=5)
                    except subprocess.TimeoutExpired:process.kill();process.wait()
        result['gpu_after']=subprocess.check_output(['nvidia-smi','--query-gpu=memory.used,memory.free,utilization.gpu','--format=csv,noheader'],text=True).strip()
        (BASE/'benchmark-results.cpu-gpu.json').write_text(json.dumps(result,indent=2)+'\n')
    finally:
        try:
            if lease is not None:agent.call('resource.release',{'id':lease},timeout=15)
        finally:agent.close()

if __name__=='__main__':main()
