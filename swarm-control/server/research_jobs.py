"""Fixed-profile private analyses using the same durable inbox as test requests."""
import copy
import datetime
import json
import os
from pathlib import Path
import signal
import tarfile
import tempfile
import subprocess
import threading
import time
import uuid
from distributed import DistributedTests, now

CONFIG=Path.home()/'.config/swarm-control/research-jobs.json'


def profiles():
    if not CONFIG.exists():return {}
    stat=CONFIG.stat()
    if CONFIG.is_symlink() or stat.st_uid!=os.getuid() or stat.st_mode&0o077:raise ValueError('Research configuration must be owner-only')
    value=json.loads(CONFIG.read_text())
    if not isinstance(value,dict) or len(value)>16:raise ValueError('Invalid research profiles')
    return value


class ResearchRun(DistributedTests):
    def __init__(self,root,state,profile):
        super().__init__(root,state,recover=False)
        self.profile=profile
        self.config=copy.deepcopy(profiles()[profile])
        self.process=None
        # Reuse the run history shape, without private source paths or results.

    def start(self,force=False,launch=False,selection=None):
        with self.lock:
            self.run={'id':datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'-'+uuid.uuid4().hex[:8],
                      'kind':'research','profile':self.profile,'selection':selection,'status':'queued',
                      'started':now(),'finished':None,'hosts':[],'repositories':{},'scope':'Private streamed DNA analysis; operational progress only'}
            self.running=True;self.save();return True

    def execute(self):
        try:
            import sys
            sys.path.insert(0,str(self.root))
            from scripts.genomics import private_run
            config=self.config
            if set(config)!={'qc','annotations','output'}:raise ValueError('Invalid research profile')
            inputs={key:Path(config[key]).resolve(strict=True) for key in ('qc','annotations')}
            for path in inputs.values():
                if not path.is_file():raise ValueError('Input unavailable')
            folder=private_run(Path(config['output']))
            revision=subprocess.check_output(['git','-C',str(self.root),'rev-parse','HEAD'],text=True).strip()
            source=folder/'runner';source.mkdir(mode=0o700)
            with tempfile.TemporaryFile() as archive:
                subprocess.run(['git','-C',str(self.root),'archive',revision,'scripts','docker_utils','swarm-control/server'],stdout=archive,check=True)
                archive.seek(0)
                with tarfile.open(fileobj=archive) as bundle:bundle.extractall(source,filter='data')
            self.run['source_revision']=revision
            command=[os.getenv('SWARM_RESEARCH_PYTHON',str(Path.home()/'.local/share/swarm-control/venv/bin/python')),
                     '-m','scripts.genomics_distributed','--qc',str(inputs['qc']),'--annotations',str(inputs['annotations']),
                     '--output',config['output'],'--run-directory',str(folder),'--group',self.run['id'],'--slots','0']
            with self.lock:
                if self.run.get('stop_requested'):return
                self.run.update(status='running');self.save()
            # Output, exceptions and individual-level reports stay on the data disk.
            with (folder/'supervisor.log').open('x') as log:
                os.chmod(folder/'supervisor.log',0o600)
                with self.lock:
                    self.process=subprocess.Popen(command,cwd=source,stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
                    if self.run.get('stop_requested'):os.killpg(self.process.pid,signal.SIGTERM)
                deadline=time.monotonic()+24*3600
                while self.process.poll() is None:
                    if time.monotonic()>deadline:
                        self.stop()
                    if self.run.get('stop_requested'):
                        try:self.process.wait(timeout=30)
                        except subprocess.TimeoutExpired:os.killpg(self.process.pid,signal.SIGKILL)
                    self.read_progress(folder)
                    time.sleep(.5)
                self.read_progress(folder)
                self.run['status']='stopped' if self.run.get('stop_requested') else 'passed' if self.process.returncode==0 else 'failed'
        except Exception:
            self.run.update(status='failed',error='Private research job failed; inspect owner-only reports on the data disk')
        finally:
            with self.lock:
                if self.run.get('stop_requested'):self.run['status']='stopped'
                self.run['finished']=now();self.running=False;self.save()

    def read_progress(self,folder):
        try:
            state=json.loads((folder/'state.json').read_text())
            jobs=list(state.get('jobs',{}).values())
            with self.lock:
                self.run['progress']={'total':len(jobs),'completed':sum(j.get('status')=='passed' for j in jobs),
                    'failed':sum(j.get('status')=='failed' for j in jobs)}
                hosts={}
                for index,job in enumerate(jobs):
                    if not job.get('host'):continue
                    host=hosts.setdefault(job['host'],{'host':job['host'],'status':'passed','jobs':{},'active_tasks':[]})
                    status=job.get('status')
                    status=status if status in ('running','passed','failed','stopped') else 'queued'
                    key='batch-'+str(index)
                    phase='waiting_for_capacity' if job.get('status')=='waiting_capacity' else 'test'
                    host['jobs'][key]={'name':'DNA batch','status':status,'phase':phase,'cpus':1,'memory_mb':512,
                                       'reused':bool(job.get('reused')),'cpu_seconds':job.get('cpu_seconds'),
                                       'duration_seconds':job.get('elapsed_seconds')}
                    if status in ('running','queued'):host['status']='running';host['active_tasks'].append(key)
                    elif status=='failed' and host['status']!='running':host['status']='failed'
                self.run['hosts']=list(hosts.values())
                self.save()
        except (OSError,ValueError):pass

    def stop(self):
        with self.lock:
            if not self.running or self.run.get('stop_requested'):return False
            self.run.update(stop_requested=True,status='stopping');self.save()
            if self.process and self.process.poll() is None:os.killpg(self.process.pid,signal.SIGTERM)
            return True
