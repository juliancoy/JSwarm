"""Compare persistent socket payloads with imported shared pages; same control flow."""
import array
import argparse
import concurrent.futures as futures
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
sys.path[:0]=[str(ROOT/'JSwarm'),str(ROOT/'JSwarm/scripts'),str(ROOT/'JSwarm/swarm-control/server')]
from swarm_gpu_memory import Session,request
from agent_client import AgentClient,CONFIG
from scripts.launcher_discovery import resolve_endpoint
BARRIER=None
WORDS=None


def client(task):
    path,digest,transport,count,batches,seed,control,verification=task
    indices=[((i+seed)*104729)%len(WORDS) for i in range(count)]
    expected=[WORDS[i] for i in indices]
    started=time.perf_counter()
    with Session(path,digest,transport,control) as session:
        setup=time.perf_counter()-started
        for i,index in enumerate(indices):session.inputs[i]=index
        for _ in range(3):
            session.gather(count)
            assert session.outputs[:count].tolist()==expected
        BARRIER.wait(timeout=30)
        samples=[]
        for iteration in range(batches):
            # Vary one index so a stale completion cannot pass validation.
            index=((seed+iteration)*7919)%len(WORDS)
            session.inputs[0]=index;expected[0]=WORDS[index]
            start=time.perf_counter();session.gather(count);samples.append(time.perf_counter()-start)
            if verification=='full':assert session.outputs[:count].tolist()==expected
            else:assert session.outputs[0]==expected[0] and session.outputs[count-1]==expected[-1]
        completed_at=time.perf_counter()
        assert session.outputs[:count].tolist()==expected
    return samples,setup,completed_at


def percentile(values,fraction):
    return sorted(values)[min(len(values)-1,int((len(values)-1)*fraction))]


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--verification',choices=('full','sampled'),default='sampled')
    parser.add_argument('--controls',default='native')
    parser.add_argument('--spin-us',type=int,default=0)
    parser.add_argument('--profile',action='store_true')
    parser.add_argument('--clients',default='1,4,8')
    parser.add_argument('--counts',default='64,4096')
    parser.add_argument('--transports',default='')
    parser.add_argument('--output',type=Path,default=Path(__file__).with_name('benchmark-results.shared.json'))
    args=parser.parse_args()
    global BARRIER,WORDS
    config=json.loads(CONFIG.read_text());name=config['name'];endpoint,_=resolve_endpoint(name)
    agent=AgentClient({'host':name,'agent_endpoint':endpoint,'local':True});lease=uuid.uuid4().hex
    result={'scope':'64 MiB synthetic warmed uint32 gather, not alignment; persistent sessions for BOTH transports; latency excludes output verification, throughput includes it; three trials; GPU not exclusive',
            'verification':args.verification,'timing':'warmups fully verified; timed batches verify all words in full mode or changing first/last words in sampled mode; final full verification, session teardown and worker teardown excluded',
            'gpu':subprocess.check_output(['nvidia-smi','--query-gpu=name,driver_version,memory.used,memory.free,utilization.gpu','--format=csv,noheader'],text=True).strip(),'rows':[]}
    try:
        admission=agent.call('resource.acquire',{'id':lease,'group':'gpu-shared-memory-benchmark','cpus':4,'memory_mb':1024,'container':'swarm-job-'+lease,'sink':'gpu','priority':-100},timeout=15)
        if not admission.get('acquired'):raise RuntimeError('No capacity for bounded benchmark')
        with tempfile.TemporaryDirectory(prefix='gmz-',dir='/tmp') as tmp:
            folder=Path(tmp);source=folder/'reference'
            WORDS=array.array('I',range(65536))*256
            source.write_bytes(WORDS.tobytes());source.chmod(0o400)
            digest=hashlib.sha256(source.read_bytes()).hexdigest()
            for clients in map(int,args.clients.split(',')):
                transports=args.transports.split(',') if args.transports else (('socket','shared') if clients==4 else ('shared','socket'))
                configurations=[(t,c) for t in transports for c in (args.controls.split(',') if t=='shared' else ['json'])]
                for transport,control in configurations:
                    key=f'{clients}-{transport}-{control}';ready=folder/(key+'.json')
                    command=[sys.executable,str(ROOT/'JSwarm/scripts/swarm_gpu_memory.py'),'--source',str(source),'--sha256',digest,'--directory',str(folder/key),'--ready',str(ready),'--budget',str(len(WORDS)*4),'--segment',str(16*1024**2),'--parallelism','4','--max-sessions','16','--spin-us',str(args.spin_us)]
                    if args.profile:command.append('--profile')
                    with (folder/(key+'.err')).open('w+') as err:
                        process=subprocess.Popen(command,stderr=err)
                        try:
                            deadline=time.monotonic()+20
                            while not ready.exists() and process.poll() is None and time.monotonic()<deadline:time.sleep(.01)
                            if not ready.exists():err.seek(0);raise RuntimeError(err.read())
                            owner=json.loads(ready.read_text());path=owner['socket']
                            for count in map(int,args.counts.split(',')):
                                before=request(path,digest);trials=[];samples=[];setups=[]
                                def owner_cpu():
                                    fields=Path(f'/proc/{process.pid}/stat').read_text().rsplit(')',1)[1].split()
                                    return (int(fields[11])+int(fields[12]))/os.sysconf('SC_CLK_TCK')
                                cpu_before=owner_cpu()
                                for trial in range(3):
                                    BARRIER=mp.get_context('fork').Barrier(clients+1)
                                    with futures.ProcessPoolExecutor(max_workers=clients,mp_context=mp.get_context('fork')) as pool:
                                        pending=[pool.submit(client,(path,digest,transport,count,100,i+trial*clients,control,args.verification)) for i in range(clients)]
                                        BARRIER.wait(timeout=30);start=time.perf_counter();finished=[]
                                        for job in pending:
                                            timings,setup,completed_at=job.result(timeout=30);samples.extend(timings);setups.append(setup);finished.append(completed_at)
                                        trials.append(max(finished)-start)
                                after=request(path,digest)
                                completed=after[transport+'_batches']-before[transport+'_batches']
                                socket_bytes=after['socket_payload_bytes']-before['socket_payload_bytes']
                                assert completed==clients*3*103
                                assert socket_bytes==(0 if transport=='shared' else completed*count*8)
                                assert after['reference_upload_bytes']==len(WORDS)*4
                                row={'transport':transport,'control':control,'verification':args.verification,'spin_us':args.spin_us,'owner_cpu_seconds_including_setup':owner_cpu()-cpu_before,'clients':clients,'batch_words':count,'batches_per_client_per_trial':100,'trial_seconds':trials,
                                     'lookups_per_s':clients*100*count/statistics.median(trials),
                                     'latency_mean_ms':statistics.mean(samples)*1000,'latency_p50_ms':statistics.median(samples)*1000,'latency_p95_ms':percentile(samples,.95)*1000,
                                     'session_setup_p50_ms':statistics.median(setups)*1000,
                                     'socket_payload_bytes_including_warmup':socket_bytes,'verified_batches_including_warmup':completed,
                                     'reference_vram_bytes':after['reference_allocation_bytes'],'reference_upload_bytes':after['reference_upload_bytes'],
                                     'query_upload_copy_bytes':after['query_upload_copy_bytes'],'result_readback_copy_bytes':after['result_readback_copy_bytes'],
                                     'configured_parallelism':after['parallelism'],'session_slots':after['session_slots'],'peak_pending_submissions':after['peak_pending_submissions'],
                                     'native_profile':{key:after['profile'][key]-before['profile'][key] for key in after['profile']}}
                                result['rows'].append(row);print(json.dumps(row),flush=True)
                        finally:
                            process.terminate()
                            try:process.wait(timeout=5)
                            except subprocess.TimeoutExpired:process.kill();process.wait()
        result['gpu_after']=subprocess.check_output(['nvidia-smi','--query-gpu=memory.used,memory.free,utilization.gpu','--format=csv,noheader'],text=True).strip()
        args.output.write_text(json.dumps(result,indent=2)+'\n')
    finally:
        try:agent.call('resource.release',{'id':lease},timeout=15)
        finally:agent.close()

if __name__=='__main__':main()
