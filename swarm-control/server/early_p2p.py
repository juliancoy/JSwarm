"""Run the coordinated browser check alongside ordinary file jobs."""
import copy
import threading
from distributed_worker import SUITES
from suite_cache import SuiteCache


class EarlyP2P:
    def __init__(self, service, hosts, images, enabled):
        self.service,self.hosts,self.images=service,hosts,images
        self.done=threading.Event();self.thread=None
        if enabled:
            service.run['p2p_early']=True
            self.thread=threading.Thread(target=self.execute,daemon=True,name='early-p2p')
            self.thread.start()
        else:self.done.set()

    def execute(self):
        service,run=self.service,self.service.run
        try:
            if len(self.hosts)>=2:
                self.images.ensure(SUITES['p2p']['image'],self.hosts)
                with service.lock:
                    peer_plan=copy.deepcopy(run)
                    for host in peer_plan['hosts']:
                        host['jobs']={k:v for k,v in host.get('jobs',{}).items() if k=='p2p'}
                        host['images']=dict(self.images.host_images.get(host['host'],{}))
                SuiteCache(service.root,service.folder,SUITES).apply(peer_plan,self.images,force=run.get('force',False))
                with service.lock:
                    for host in self.hosts:
                        original=next(h for h in peer_plan['hosts'] if h['host']==host['host'])
                        host['jobs']['p2p']=original['jobs']['p2p']
                    service.save()
            service.network(run,self.images,parallel=True)
        except Exception as error:
            with service.lock:
                run['p2p']={'status':'failed','error':str(error)[:500]}
                for host in self.hosts:
                    host['jobs']['p2p'].update(status='failed',failure_details=[str(error)[:500]])
                service.save()
        finally:self.done.set()

    def join(self):
        if self.thread:self.thread.join()
