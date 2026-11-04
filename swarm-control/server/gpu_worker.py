"""Run a signed fixed CUDA kernel check on verified GPU-container workers."""
import concurrent.futures
import threading
import time
from agent_client import AgentClient
from distributed_worker import PYTHON


class GpuRun:
    def __init__(self,service,hosts,images,enabled):
        self.service,self.images=service,images
        self.hosts=[];self.thread=None
        if not enabled:return
        coverage=[]
        for host in hosts:
            hardware=host.get('agent_status',{}).get('hardware',{})
            nvidia=any(g.get('vendor')=='NVIDIA' for g in hardware.get('gpus',[]))
            ready=nvidia and hardware.get('cuda_container_runtime') is True
            coverage.append({'host':host['host'],'eligible':ready,'reason':
                'CUDA container runtime detected' if ready else 'NVIDIA runtime missing' if nvidia else 'No CUDA GPU detected'})
            if ready:
                host['suites'].append('gpu')
                host['jobs']['gpu']={'name':'CUDA container kernel validation','status':'queued','logs':[]}
                self.hosts.append(host)
        service.run['gpu']={'coverage':coverage,'status':'running' if self.hosts else 'unavailable'}
        self.thread=threading.Thread(target=self.execute,daemon=True,name='gpu-validation');self.thread.start()

    def execute(self):
        def check(host):
            job=host['jobs']['gpu'];started=time.monotonic()
            try:
                with self.service.lock:
                    job.update(status='running',phase='CUDA kernel');self.service.save()
                if self.service.run.get('stop_requested'):raise InterruptedError('Run stopped')
                self.images.ensure(PYTHON,[host])
                client=AgentClient(host)
                try:
                    deadline=time.monotonic()+1800
                    while True:
                        if self.service.run.get('stop_requested'):raise InterruptedError('Run stopped')
                        result=client.call('gpu.probe',timeout=180)
                        if not result.get('waiting_capacity'):break
                        with self.service.lock:job.update(phase='waiting_for_capacity');self.service.save()
                        if time.monotonic()>deadline:raise RuntimeError('GPU CPU/memory admission deadline exceeded')
                        time.sleep(.5)
                finally:client.close()
                if result.get('status')!='passed' or not result.get('devices') or any(device.get('kernel_result')!=42 for device in result['devices']):raise RuntimeError('Missing GPU compute validation or incorrect kernel result')
                with self.service.lock:
                    job.update(status='passed',gpu=result,test_counts={'tests':len(result['devices']),'passed':len(result['devices']),'failed':0})
                    job['logs']=[device['name']+' · GPU kernel returned '+str(device['kernel_result']) for device in result['devices']]
            except Exception as error:
                with self.service.lock:job.update(status='failed',failure_details=[str(error)[:800]])
            finally:
                with self.service.lock:
                    job['duration_seconds']=round(time.monotonic()-started,3);self.service.save()
        with concurrent.futures.ThreadPoolExecutor(max_workers=max(1,len(self.hosts))) as pool:list(pool.map(check,self.hosts))
        with self.service.lock:
            self.service.run['gpu']['status']='passed' if self.hosts and all(h['jobs']['gpu']['status']=='passed' for h in self.hosts) else 'failed' if self.hosts else 'unavailable'
            self.service.save()

    def join(self):
        if self.thread:self.thread.join()
