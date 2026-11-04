"""Load mounted coordinator code for each new run without restarting the API."""
import importlib
import sys
import threading
import copy
import time


class LiveDistributedTests:
    def __init__(self):
        self.lock = threading.RLock()
        self.runner = self.load()
        self.reruns={}
        # No in-memory runner survives restart: do not silently repeat private jobs.
        import json
        for path in self.runner.folder.glob('*.json'):
            try:
                record=json.loads(path.read_text())
                if not record.get('finished') and record.get('status') in ('queued','running','preparing','stopping'):
                    record.update(status='interrupted',finished=__import__('distributed').now())
                    pending=path.with_suffix('.tmp');pending.write_text(json.dumps(record));pending.chmod(0o600);pending.replace(path)
            except (OSError,ValueError):continue
        from test_requests import TestRequests
        self.requests=TestRequests(self)
        from snapshot_staging import SnapshotStaging
        self.staging=SnapshotStaging(self.runner.root)
        from retry_jobs import run as retry_jobs
        threading.Thread(target=retry_jobs,args=(self,),daemon=True).start()

    @staticmethod
    def load():
        return importlib.import_module('distributed').DistributedTests()

    def view(self, ident=None):
        with self.lock:
            selected=self.reruns.get(ident,self.runner)
            result=selected.view(ident)
            result['snapshot_staging']=self.staging.view()
            result['draining']=(self.runner.folder/'DRAIN').exists()
            result['active_runs']=[{'id':r.run['id'],'status':r.run['status'],'kind':r.run.get('kind','tests')} for r in self.active()]
            result['active_reruns']=[r.run['id'] for r in self.reruns.values() if r.running]
            return result

    def cpu_activity(self, ident=None):
        """Bounded scheduling metadata; telemetry never copies test output/artifacts."""
        with self.lock:
            selected=self.reruns.get(ident,self.runner)
            with selected.lock:
                hosts=[]
                if selected.running:
                    for host in (selected.run or {}).get('hosts',[]):
                        hosts.append({'host':host['host'],'status':host.get('status'),
                            'active_tasks':bool(host.get('active_tasks')),
                            'dependency_preparation':{k:{'status':v.get('status')} for k,v in host.get('dependency_preparation',{}).items()},
                            'jobs':{key:{k:job.get(k) for k in ('status','phase','cpus')} for key,job in host.get('jobs',{}).items()
                                    if job.get('status') in ('queued','running')}})
                return {'running':selected.running,'run':{'hosts':hosts},
                        'active_reruns':[key for key,runner in self.reruns.items() if runner.running]}

    def artifact(self, run, host, suite, name):
        with self.lock:
            return self.runner.artifact(run, host, suite, name)

    def active(self):
        return [r for r in [self.runner,*self.reruns.values()] if r.running]

    @staticmethod
    def exclusive(selection):
        return not selection or bool(set(selection.get('suites',[])) & {'p2p','mobile','gpu'})

    def create_run(self,force=False,selection=None):
        with self.lock:
            if (self.runner.folder/'DRAIN').exists():return None
            active=self.active()
            if len(active)>=8:return None
            if self.exclusive(selection) and any(self.exclusive(r.run.get('selection')) for r in active):
                return None
            from distributed import DistributedTests
            if selection and selection.get('research'):
                from research_jobs import ResearchRun
                runner=ResearchRun(self.runner.root,self.runner.folder,selection['research'])
            else:runner=DistributedTests(self.runner.root,self.runner.folder,recover=False)
            runner.publication_lock=self.runner.publication_lock
            runner.start(force=force,launch=False,selection=selection)
            self.reruns={key:r for key,r in self.reruns.items() if r.running}
            if self.runner.running:self.reruns[runner.run['id']]=runner
            else:self.runner=runner
            return runner

    def start(self,force=False):
        runner=self.create_run(force=force)
        if runner is None:return False
        threading.Thread(target=runner.execute,daemon=True).start()
        return True

    def stop(self):
        with self.lock:
            return self.runner.stop()

    def rerun(self,parent_run_id,host=None,suite=None,failed_only=False):
        with self.lock:
            if sum(r.running for r in self.reruns.values())>=10:raise ValueError('At most ten reruns may be queued')
            parent=self.view(parent_run_id)['run']
            selected=[]
            for original in parent['hosts']:
                if host and original['host']!=host:continue
                jobs={key:job for key,job in original['jobs'].items() if (not suite or key==suite)
                      and (not failed_only or key!='p2p') and (not failed_only or job['status'] in ('failed','timed_out','unavailable','interrupted'))}
                if not jobs:continue
                if 'mobile' in jobs:raise ValueError('Use request_tests with suites mobile for an Android rerun')
                if 'p2p' in jobs:raise ValueError('P2P requires a full distributed run; select an ordinary suite')
                worker={key:copy.deepcopy(original[key]) for key in ('host','local') if key in original}
                worker.update(status='queued',suites=list(jobs),jobs={key:{'name':job['name'],'status':'queued','phase':None,'logs':[]} for key,job in jobs.items()})
                selected.append(worker)
            if not selected:raise ValueError('No matching suites to rerun')
            paths=[];suites=[]
            for worker in selected:
                for task in worker['jobs']:
                    original=next(h for h in parent['hosts'] if h['host']==worker['host'])['jobs'][task]
                    base=original.get('suite') or task.split('--',1)[0]
                    file=original.get('file')
                    if file:
                        from work_queue import FILES
                        repo,prefix,_=FILES[base]
                        from pathlib import PurePosixPath
                        paths.append(str(PurePosixPath(repo)/prefix/file))
                    else:suites.append(base)
            selection={'paths':sorted(set(paths)),'suites':sorted(set(suites))}
            # Fresh reruns use the same resource admission as ordinary requests.
            runner=self.create_run(force=True,selection=selection)
            if runner is None:raise ValueError('Concurrent run limit reached or an exclusive hardware job is active')
            runner.run['parent_run_id']=parent_run_id;runner.save()
            threading.Thread(target=runner.execute,daemon=True).start()
            return runner.run['id']

    def retry_pinned(self,parent,ticket):
        """Retry one failed logical job against its existing published commits."""
        task=ticket['task']
        specs=None
        def recover(ident):
            nonlocal specs
            if specs is None:
                from work_queue import catalog
                from distributed_worker import SUITES
                specs=copy.deepcopy(SUITES);published=catalog(self.runner.root,specs,parent['repositories'])
                specs={key:specs[key] for key in published}
            if ident not in specs:raise ValueError('Pinned task '+ident+' is absent from source '+parent.get('source_sha256','unknown'))
            spec=specs[ident]
            return {'suite':spec.get('base',ident),'file':spec.get('file'),'check':spec.get('check')}
        matches=[(h,j) for h in parent.get('hosts',[]) for key,j in h.get('jobs',{}).items() if key==task]
        if not matches:
            matches=[({'host':'unassigned'},recover(task))]
        original,job=matches[-1];base=job.get('suite') or task.split('--',1)[0]
        if base in ('p2p','mobile','gpu'):raise ValueError('Specialized hardware jobs require their explicit retry workflow')
        if job.get('file'):
            from work_queue import FILES
            from pathlib import PurePosixPath
            repo,prefix,_=FILES[base];selection={'paths':[str(PurePosixPath(repo)/prefix/job['file'])],'suites':[]}
        else:selection={'paths':[],'suites':[base]}
        for sibling in ticket.get('batch',[]):
            if sibling['task']==task:continue
            rows=[j for h in parent.get('hosts',[]) for key,j in h.get('jobs',{}).items() if key==sibling['task']]
            if not rows:rows=[recover(sibling['task'])]
            item=rows[-1];component=item.get('suite') or sibling['task'].split('--',1)[0]
            if component in ('p2p','mobile','gpu'):raise ValueError('Specialized hardware jobs require their explicit retry workflow')
            if item.get('file'):
                from work_queue import FILES
                from pathlib import PurePosixPath
                repo,prefix,_=FILES[component];selection['paths'].append(str(PurePosixPath(repo)/prefix/item['file']))
            else:selection['suites'].append(component)
        selection={key:sorted(set(values)) for key,values in selection.items()}
        selection['task_ids']=[t['task'] for t in ticket.get('batch',[ticket])]
        from github_sources import source_digest
        repositories=copy.deepcopy(parent['repositories'])
        if source_digest(repositories)!=parent['source_sha256']:raise ValueError('Pinned source identity mismatch')
        runner=self.create_run(force=any(t['attempts']>0 for t in ticket.get('batch',[ticket])),selection=selection)
        if runner is None:raise RuntimeError('Retry waiting for ordinary admission')
        runner.run.update(pinned_repositories=repositories,retry_family=parent.get('retry_family',parent['id']),
                          retry_ticket=ticket['id'],retry_attempt=ticket['attempts']+1,parent_run_id=parent['id'],
                          retry_tickets=[t['id'] for t in ticket.get('batch',[ticket])],
                          avoid_workers=[original['host']],scope='Automatic bounded retry of the original published commits; no commit, push or deployment.')
        runner.save();return runner

    def stop_run(self,ident):
        with self.lock:
            runner=self.reruns.get(ident)
            if runner is None:
                if self.runner.run.get('id')!=ident:return False
                runner=self.runner
            if runner.run.get('status')=='queued':
                runner.run['stop_requested']=True;runner.save();return True
            return runner.stop()
