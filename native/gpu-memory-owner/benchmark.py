"""Bounded concurrent gather benchmark; not a minimap2 alignment benchmark."""
import array
import concurrent.futures as futures
import hashlib
import json
import multiprocessing as mp
from pathlib import Path
import statistics
import subprocess
import sys
import tempfile
import time
import uuid

ROOT=Path(__file__).resolve().parents[3]
sys.path[:0]=[str(ROOT/'JSwarm'),str(ROOT/'JSwarm/scripts'),str(ROOT/'JSwarm/swarm-control/server')]
from swarm_gpu_memory import request
from agent_client import AgentClient,CONFIG
from scripts.launcher_discovery import resolve_endpoint
BARRIER=None
WORDS=None


def client(task):
    path,digest,count,batches,seed=task
    indices=[((i+seed)*104729)%len(WORDS) for i in range(count)]
    expected=[WORDS[i] for i in indices]
    def run():
        return request(path,digest,indices) if path else [WORDS[i] for i in indices]
    for _ in range(3):assert run()==expected
    BARRIER.wait(timeout=30)
    samples=[]
    for _ in range(batches):
        start=time.perf_counter();result=run();samples.append(time.perf_counter()-start)
        assert result==expected
    return samples


def percentile(values,fraction):
    return sorted(values)[min(len(values)-1,int((len(values)-1)*fraction))]


def main():
    global BARRIER,WORDS
    config=json.loads(CONFIG.read_text());name=config['name'];endpoint,_=resolve_endpoint(name)
    agent=AgentClient({'host':name,'agent_endpoint':endpoint,'local':True});lease=uuid.uuid4().hex
    result={'scope':'synthetic uint32 gather; not alignment; host wall timing includes socket, Python, GPU queue/fence and returned data; three trials; shared GPU activity uncontrolled',
            'gpu':subprocess.check_output(['nvidia-smi','--query-gpu=name,driver_version,memory.used,memory.free,utilization.gpu','--format=csv,noheader'],text=True).strip(),'rows':[]}
    try:
        admit=agent.call('resource.acquire',{'id':lease,'group':'gpu-owner-benchmark','cpus':4,'memory_mb':1024,'container':'swarm-job-'+lease,'sink':'gpu','priority':-100},timeout=15)
        if not admit.get('acquired'):raise RuntimeError('No capacity for bounded benchmark')
        with tempfile.TemporaryDirectory(prefix='gmb-',dir='/tmp') as tmp:
            folder=Path(tmp);source=folder/'reference'
            WORDS=array.array('I',range(65536))*256 # 64 MiB, no genomic cache input
            source.write_bytes(WORDS.tobytes());source.chmod(0o400)
            digest=hashlib.sha256(source.read_bytes()).hexdigest()
            for clients in (1,4,8):
                modes=('shared1','shared4','shared8','private') if clients%2 else ('private','shared8','shared4','shared1')
                for mode in modes:
                    processes=[];files=[];infos=[];startup=time.perf_counter()
                    try:
                        owners=clients if mode=='private' else 1
                        slots=1 if mode=='private' else int(mode.removeprefix('shared'))
                        for i in range(owners):
                            key=f'{clients}-{mode}-{i}';ready=folder/(key+'.json')
                            err=(folder/(key+'.err')).open('w+');files.append(err)
                            cmd=[sys.executable,str(ROOT/'JSwarm/scripts/swarm_gpu_memory.py'),'--source',str(source),'--sha256',digest,'--directory',str(folder/key),'--ready',str(ready),'--budget',str(len(WORDS)*4),'--segment',str(16*1024**2),'--parallelism',str(slots)]
                            process=subprocess.Popen(cmd,stderr=err);processes.append(process)
                            deadline=time.monotonic()+20
                            while not ready.exists() and process.poll() is None and time.monotonic()<deadline:time.sleep(.01)
                            if not ready.exists():err.seek(0);raise RuntimeError(err.read())
                            infos.append(json.loads(ready.read_text()))
                        startup=time.perf_counter()-startup
                        for count in (64,4096):
                            trials=[];samples=[]
                            for trial in range(3):
                                BARRIER=mp.get_context('fork').Barrier(clients+1)
                                with futures.ProcessPoolExecutor(max_workers=clients,mp_context=mp.get_context('fork')) as pool:
                                    pending=[pool.submit(client,(infos[i if mode=='private' else 0]['socket'],digest,count,100,i+trial*clients)) for i in range(clients)]
                                    BARRIER.wait(timeout=30);start=time.perf_counter()
                                    for job in pending:samples.extend(job.result(timeout=30))
                                    trials.append(time.perf_counter()-start)
                            snapshots=[request(info['socket'],digest) for info in infos]
                            assert all(info['reference_upload_bytes']==len(WORDS)*4 for info in snapshots)
                            row={'mode':mode,'clients':clients,'batch_words':count,'batches_per_client_per_trial':100,'trial_seconds':trials,
                                 'batches_per_s':clients*100/statistics.median(trials),'lookups_per_s':clients*100*count/statistics.median(trials),
                                 'latency_p50_ms':statistics.median(samples)*1000,'latency_p95_ms':percentile(samples,.95)*1000,
                                 'reference_vram_bytes':sum(info['reference_allocation_bytes'] for info in infos),'reference_upload_bytes':sum(info['reference_upload_bytes'] for info in infos),'all_owners_startup_s':startup,
                                 'configured_slots_per_owner':slots,'peak_pending_submissions_per_owner':[info['peak_pending_submissions'] for info in snapshots]}
                            result['rows'].append(row)
                            print(json.dumps(row),flush=True)
                    finally:
                        for process in processes:process.terminate()
                        for process in processes:
                            try:process.wait(timeout=5)
                            except subprocess.TimeoutExpired:process.kill();process.wait()
                        for file in files:file.close()
        result['gpu_after']=subprocess.check_output(['nvidia-smi','--query-gpu=memory.used,memory.free,utilization.gpu','--format=csv,noheader'],text=True).strip()
        Path(__file__).with_name('benchmark-results.parallel.json').write_text(json.dumps(result,indent=2)+'\n')
    finally:
        try:agent.call('resource.release',{'id':lease},timeout=15)
        finally:agent.close()

if __name__=='__main__':main()
