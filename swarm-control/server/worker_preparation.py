"""One bounded installer per worker; tests only receive ready components."""
import threading
import time

from distributed_worker import SUITES, emit, clean

P2P_RESERVATION = {'cpus': 2, 'memory_mb': 2048}
PREPARATION_RESERVATION = {'cpus': 1, 'memory_mb': 1536}


def execution_capacity(capacity, preparing=False, p2p=False):
    return {key:max(0, capacity[key] - (PREPARATION_RESERVATION[key] if preparing else 0)
                    - (P2P_RESERVATION[key] if p2p else 0)) for key in ('cpus','memory_mb')}


class Preparation:
    def __init__(self, work, manifest, target, dependencies, capacity):
        self.work,self.manifest,self.target,self.dependencies,self.capacity=work,manifest,target,dependencies,capacity
        self.ready=set();self.results={};self.running=True;self.lock=threading.Lock();self.stop=threading.Event()
        self.dependencies.cancel=self.stop
        self.thread=threading.Thread(target=self.prepare,daemon=True)

    def p2p(self):
        return self.manifest.get('p2p_parallel') and not (self.work.parent/'P2P_FINISHED').exists()

    def prepare(self):
        try:
            for base in sorted(self.target['suites'], key=lambda name: bool(SUITES[name].get('prepare'))):
                requires=bool(SUITES[base].get('prepare'))
                while True:
                    if self.stop.is_set() or (self.work.parent/'STOP').exists():return
                    available=execution_capacity(self.capacity,p2p=self.p2p())
                    if not requires or self.capacity['memory_mb']<PREPARATION_RESERVATION['memory_mb'] or all(available[k]>=PREPARATION_RESERVATION[k] for k in available):break
                    time.sleep(.1)
                started=time.monotonic()
                emit('dependencies',base=base,status='preparing')
                code=125
                try:
                    if requires and self.capacity['memory_mb']<PREPARATION_RESERVATION['memory_mb']:raise RuntimeError('Insufficient memory for dependency preparation')
                    spec={**SUITES[base],'base':base,'image':self.target['images'][SUITES[base]['image']],**PREPARATION_RESERVATION}
                    code=self.dependencies.ensure(base,spec)
                except Exception as error:emit('log',line='Dependency setup '+base+': '+clean(str(error)))
                with self.lock:
                    self.results[base]=code
                    self.ready.add(base)
                emit('dependencies',base=base,status='ready' if code==0 else 'failed',exit_code=code,
                     duration_seconds=round(time.monotonic()-started,3))
        finally:
            self.running=False
            emit('dependencies',status='complete')

    def start(self):self.thread.start()

    def close(self):
        self.stop.set()
        self.thread.join()
