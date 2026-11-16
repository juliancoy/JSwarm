"""Interleaved GPU A/B: old recording vs cached mapped vs cached staged I/O."""
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
import benchmark_cpu_gpu as paired
from swarm_gpu_memory import request
from agent_client import AgentClient,CONFIG
from scripts.launcher_discovery import resolve_endpoint

BASE=Path(__file__).resolve().parent
ROOT=BASE.parents[2]
CONFIGURATIONS={'baseline':('off','mapped','gather.spv'),'cached-mapped':('on','mapped','gather.spv'),'cached-staged':('on','staged','gather.spv'),
                'cached-baseline-shader':('on','mapped','gather-baseline.spv'),'cached-optimized-shader':('on','mapped','gather-shift.spv')}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--configs',default='baseline,cached-mapped,cached-staged')
    parser.add_argument('--profile',action='store_true')
    parser.add_argument('--output',type=Path,default=BASE/'benchmark-results.optimization.json')
    args=parser.parse_args()
    configurations={key:CONFIGURATIONS[key] for key in args.configs.split(',')}
    result={'scope':'64 MiB synthetic gather, not alignment; same direct-view producer/checks as paired CPU/GPU benchmark; configurations rotate order each of three trials; host not isolated',
            'gpu':subprocess.check_output(['nvidia-smi','--query-gpu=name,driver_version,memory.used,memory.free,utilization.gpu','--format=csv,noheader'],text=True).strip(),
            'load_before':os.getloadavg(),'rows':[]}
    config=json.loads(CONFIG.read_text());name=config['name'];endpoint,_=resolve_endpoint(name)
    agent=AgentClient({'host':name,'agent_endpoint':endpoint,'local':True});lease=None
    processes=[];files=[]
    try:
        for cpus in (8,4):
            lease=uuid.uuid4().hex
            admission=agent.call('resource.acquire',{'id':lease,'group':'gpu-owner-optimization-ab','cpus':cpus,'memory_mb':1024,'container':'swarm-job-'+lease,'sink':'gpu','priority':-100},timeout=15)
            if admission.get('acquired'):break
            agent.call('resource.release',{'id':lease},timeout=15);lease=None
        if lease is None:raise RuntimeError('No capacity for bounded optimization benchmark')
        result['admitted_cpu_budget']=cpus
        with tempfile.TemporaryDirectory(prefix='gmoa-',dir='/tmp') as tmp:
            folder=Path(tmp);source=folder/'reference';paired.WORDS=array.array('I',range(65536))*256
            source.write_bytes(paired.WORDS.tobytes());source.chmod(0o400)
            digest=hashlib.sha256(source.read_bytes()).hexdigest();paths={}
            # Three private benchmark owners isolate the configurations. Each
            # uploads 64 MiB once; only one configuration executes at a time.
            for label,(reuse,io,shader) in configurations.items():
                ready=folder/(label+'.json');err=(folder/(label+'.err')).open('w+');files.append(err)
                command=[sys.executable,str(ROOT/'JSwarm/scripts/swarm_gpu_memory.py'),'--source',str(source),'--sha256',digest,'--directory',str(folder/label),'--ready',str(ready),'--budget',str(len(paired.WORDS)*4),'--segment',str(16*1024**2),'--parallelism','4','--max-sessions','16','--spin-us','0','--command-reuse',reuse,'--device-io',io,'--shader',str(BASE/shader)]
                if args.profile:command.append('--profile')
                process=subprocess.Popen(command,stderr=err);processes.append(process)
                deadline=time.monotonic()+20
                while not ready.exists() and process.poll() is None and time.monotonic()<deadline:time.sleep(.01)
                if not ready.exists():err.seek(0);raise RuntimeError(err.read())
                paths[label]=json.loads(ready.read_text())['socket']
            for workers in (1,cpus):
                for pattern in ('warm','changing'):
                    for count in (4096,65536):
                        before={key:request(path,digest) for key,path in paths.items()}
                        measurements={key:{'seconds':[],'samples':[]} for key in paths}
                        for trial in range(3):
                            order=list(paths);order=order[trial:]+order[:trial]
                            for label in order:
                                paired.BARRIER=mp.get_context('fork').Barrier(workers+1)
                                with futures.ProcessPoolExecutor(max_workers=workers,mp_context=mp.get_context('fork')) as pool:
                                    pending=[pool.submit(paired.worker,('gpu',pattern,paths[label],digest,count,1+i+trial*workers)) for i in range(workers)]
                                    paired.BARRIER.wait(timeout=30);start=time.perf_counter();finished=[]
                                    for job in pending:
                                        timings,end=job.result(timeout=30);measurements[label]['samples'].extend(timings);finished.append(end)
                                    measurements[label]['seconds'].append(max(finished)-start)
                        for label,data in measurements.items():
                            after=request(paths[label],digest);old=before[label]
                            recordings=after['command_recordings']-old['command_recordings'];reuses=after['command_reuses']-old['command_reuses']
                            expected_batches=workers*3*103
                            assert after['query_batches']-old['query_batches']==expected_batches
                            assert recordings==(expected_batches if label=='baseline' else workers*3)
                            assert reuses==(0 if label=='baseline' else workers*3*102)
                            assert after['reference_upload_bytes']==len(paired.WORDS)*4 and after['socket_payload_bytes']==0
                            copied=after['query_upload_copy_bytes']-old['query_upload_copy_bytes']
                            assert copied==(expected_batches*count*4 if label=='cached-staged' else 0)
                            assert after['result_readback_copy_bytes']-old['result_readback_copy_bytes']==copied
                            samples=data['samples']
                            row={'configuration':label,'workers':workers,'pattern':pattern,'batch_words':count,
                                 'trial_seconds':data['seconds'],'lookups_per_s':workers*100*count/statistics.median(data['seconds']),
                                 'latency_p50_ms':statistics.median(samples)*1000,'latency_p95_ms':sorted(samples)[int((len(samples)-1)*.95)]*1000,
                                 'command_recordings':recordings,'command_reuses':reuses,'upload_copy_bytes':copied,'readback_copy_bytes':copied,
                                 'reference_bytes':after['reference_allocation_bytes'],'device_io_bytes':after['device_io_allocation_bytes'],
                                 'profile_delta':{key:after['profile'][key]-old['profile'][key] for key in after['profile']}}
                            result['rows'].append(row);print(json.dumps(row),flush=True)
            result['load_after']=os.getloadavg()
            args.output.write_text(json.dumps(result,indent=2)+'\n')
    finally:
        for process in processes:process.terminate()
        for process in processes:
            try:process.wait(timeout=5)
            except subprocess.TimeoutExpired:process.kill();process.wait()
        for file in files:file.close()
        try:
            if lease:agent.call('resource.release',{'id':lease},timeout=15)
        finally:agent.close()

if __name__=='__main__':main()
