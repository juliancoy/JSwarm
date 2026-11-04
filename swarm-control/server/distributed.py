"""Bounded, user-triggered application shards over authenticated launcher agents."""
from paths import WORKSPACE,STATE
import concurrent.futures
import copy
import datetime as dt
import hashlib
import json
from pathlib import Path
import re
import subprocess
import threading
import time
import uuid
from distributed_worker import SUITES
from distributed_images import ImageDistributor
from run_activity import capture
from visual_artifacts import VisualArtifacts

ROOT = WORKSPACE
STATE = STATE / 'distributed'
HISTORY_LIMIT = 10
SUCCESS = {'passed','reused'}
FINAL = {'reused','partial', 'passed', 'failed', 'unavailable', 'timed_out', 'interrupted', 'stopped'}


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def targets():
    from agent_client import CONFIG
    config=json.loads(CONFIG.read_text())
    peers=config.get('peers',{})
    if not 1<=len(peers)<=24:raise ValueError('Enroll between one and 24 launcher agents')
    return [{'host':name,'local':name==config['name']} for name in sorted(peers)]


def outcome(run, hosts, excluded):
    if run.get('selection') and run.get('granular'):
        jobs=[job for host in hosts for job in host.get('jobs',{}).values()]
        # Idle workers that lose a race to another coordinator are not failed
        # tests. Every selected task must still have a verified final result.
        return 'passed' if jobs and not run.get('pending_tasks') and all(job['status'] in SUCCESS for job in jobs) else 'failed'
    if any(job.get('status') not in SUCCESS for host in hosts for job in host.get('jobs',{}).values()):return 'failed'
    return ('failed' if any(host['status'] not in ('passed','partial') for host in hosts) else
            'partial' if (excluded and run.get('targeted')) or any(host['status']=='partial' for host in hosts) else 'passed')


def no_remote_shell(target):
    raise RuntimeError('SSH is not an execution transport; provision an authenticated launcher agent')


def plan(hosts,selection=None):
    hosts = copy.deepcopy(hosts)
    for host in hosts:
        if selection and 'p2p' not in selection.get('suites',[]):
            host.update(status='queued',suites=[],jobs={})
            continue
        host.update(status='queued', suites=['p2p'], jobs={
            'p2p': {'name': SUITES['p2p']['name'], 'status': 'queued', 'phase': None, 'logs': []}})
    return hosts




class DistributedTests:
    def __init__(self, root=ROOT, state=STATE, recover=True):
        self.root, self.folder = Path(root), Path(state)
        self.lock = threading.RLock()
        self.running = False
        self.run = None
        self.last_save = 0
        self.publication_lock=threading.Lock()
        self.folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.artifacts = VisualArtifacts(self.folder)
        self.prune()
        files = sorted(self.folder.glob('*.json'))
        if files and recover:
            try:
                self.run = json.loads(files[-1].read_text())
                if self.run.get('status') in ('queued','running', 'preparing', 'stopping'):
                    self.run.update(status='interrupted', finished=now())
                    for host in self.run['hosts']:
                        if host['status'] not in FINAL:
                            host['status'] = 'interrupted'
                        for job in host.get('jobs', {}).values():
                            if job.get('status') not in FINAL:
                                job['status'] = 'interrupted'
                    self.save()
            except (OSError, ValueError):
                self.run = None

    def prune(self):
        cutoff = time.time() - 30 * 86400
        files = sorted(self.folder.glob('*.json'), reverse=True)
        for index, path in enumerate(files):
            if index >= 100 or path.stat().st_mtime < cutoff:
                path.unlink()
                __import__('shutil').rmtree(self.folder / (path.stem + '-artifacts'), ignore_errors=True)

    def save(self):
        if not self.run:
            return
        if getattr(self, '_activity_run', None) != self.run['id']:
            self._activity_run = self.run['id']
            self._activity_snapshot = {}
        self._activity_snapshot = capture(self.run, self._activity_snapshot, now())
        from task_events import EventStore
        EventStore((self.folder.parent if self.folder.name=='distributed' else self.folder)/'compute-events.sqlite3').observe(self.run, 'application-tests', 'coordinator')
        path = self.folder / (self.run['id'] + '.json')
        temporary = path.with_suffix('.tmp')
        temporary.write_text(json.dumps(self.run))
        temporary.chmod(0o600)
        temporary.replace(path)
        self.last_save = time.monotonic()
        if self.run.get('finished'):
            from runtime_stats import record
            try:record(self.folder,self.run)
            except Exception:
                __import__('logging').exception('Unable to persist runtime statistics for %s',self.run['id'])

    def view(self, ident=None):
        with self.lock:
            current = copy.deepcopy(self.run)
            if ident and (not current or ident != current['id']):
                if not re.fullmatch(r'[0-9TZ-]+-[a-f0-9]{8}', ident):
                    raise ValueError('Invalid run ID')
                path = self.folder / (ident + '.json')
                if not path.is_file():
                    raise ValueError('Run not found')
                current = json.loads(path.read_text())
            history = []
            for path in sorted(self.folder.glob('*.json'), reverse=True)[:100]:
                try:
                    data = json.loads(path.read_text())
                    item={key: data.get(key) for key in ('id', 'started', 'finished', 'status', 'source_sha256', 'selection')}
                    jobs=[job for host in data.get('hosts',[]) for job in host.get('jobs',{}).values()]
                    item.update(suites=len(jobs),passed=sum(job.get('status')=='passed' for job in jobs),reused=sum(job.get('status')=='reused' for job in jobs),
                                failed=sum(job.get('status') in ('failed','timed_out','unavailable','interrupted') for job in jobs))
                    history.append(item)
                    if len(history)>=HISTORY_LIMIT:break
                except (OSError, ValueError):
                    pass
            return {'running': self.running, 'run': current, 'history': history, 'history_limit': HISTORY_LIMIT, 'retention_days': 30}

    def artifact(self, run, host, suite, name):
        return self.artifacts.read(run, host, suite, name)

    def start(self, selected_hosts=None, parent_run_id=None, launch=True, force=False, selection=None):
        with self.lock:
            if self.running:
                return False
            hosts = copy.deepcopy(selected_hosts) if selected_hosts is not None else plan(targets(),selection)
            self.prune()
            self.run = {'id': dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + uuid.uuid4().hex[:8],
                        'status': 'preparing', 'started': now(), 'finished': None, 'force':force,
                        'hosts': hosts, 'repositories': {}, 'source_sha256': None,
                        'scope': 'Start commits and pushes Organization, OrgPortal, PIdP and CodeCollective on their current branches; verified local agents fetch exact GitHub commits into fresh directories. Individual test files and build checks are pulled from a shared queue as CPU and memory become available. Each machine keeps one checkout and dependency setup for the run. Test containers have no network or deployment credentials. The dedicated P2P suite uses separate browser containers on authenticated agent machines, synthetic fixtures and short-lived dataset invitations; its network access is required for WebRTC.'}
            if selection:
                self.run["selection"]=selection
                self.run["scope"]="Focused request: "+json.dumps(selection)+". Publishes current sources and runs selected file jobs on available workers. No post-deployment or email checks."
            if parent_run_id:self.run.update(parent_run_id=parent_run_id,targeted=True,status='queued')
            self.running = True
            self.save()
            if launch:threading.Thread(target=self.execute, daemon=True).start()
            return True

    def stop(self):
        with self.lock:
            if not self.running or self.run.get('stop_requested'):
                return False
            self.run.update(stop_requested=True, status='stopping')
            self.save()
            threading.Thread(target=self.stop_hosts, args=(self.run,), daemon=True).start()
            return True

    def stop_hosts(self, run):
        from agent_client import AgentClient
        def stop_host(host):
            if host.get('excluded') or not host.get('agent_endpoint'):return
            try:
                result=AgentClient(host).call('job.stop',{'id':host.get('agent_job_id',run['id'])},timeout=40)
                host['stop_acknowledged']=result.get('acknowledged') is True
            except Exception as error:host['stop_error']=str(error)[:400]
        with concurrent.futures.ThreadPoolExecutor(max_workers=max(1,len(run['hosts']))) as pool:
            list(pool.map(stop_host,run['hosts']))

    def update_events(self, host, events):
        """Durable batch first; never hold the control lock during raw log fsync."""
        if not events:return
        from task_events import EventStore
        EventStore((self.folder.parent if self.folder.name=='distributed' else self.folder)/'compute-events.sqlite3').append_batch(
            [(self.run['id'],host['host'],event.get('kind','event'),event,None) for event in events])
        for event in events:self.update(host,event,persist=False,record=False)

    def update(self, host, event, *, persist=True, record=True):
        with self.lock:
            from task_events import EventStore
            if record:
                EventStore((self.folder.parent if self.folder.name=='distributed' else self.folder)/'compute-events.sqlite3').append(
                    self.run['id'], host['host'], event.get('kind', 'event'), event)
            kind = event.get('kind')
            if kind == 'host':
                host.update(observed_hostname=event.get('hostname'), cpus=event.get('cpus'), python=event.get('python'))
            if kind=='source':
                host.setdefault('source_sync',{})[event['repository']]={key:event[key] for key in ('commit','status') if key in event}
                host['status']='running' if len(host['source_sync'])==len(self.run['repositories']) and all(row.get('status')=='verified' for row in host['source_sync'].values()) else 'preparing'
            if kind=='dependencies':
                host.setdefault('dependency_preparation',{})[event.get('base','overall')]={key:value for key,value in event.items() if key in ('status','exit_code','duration_seconds')}
            if kind == 'log' and event.get('suite') not in host.get('jobs',{}):
                host.setdefault('logs',[]).append(str(event.get('line',''))[:2000])
                host['logs']=host['logs'][-80:]
            suite = event.get('suite')
            if suite in host['jobs']:
                job = host['jobs'][suite]
                if kind in ('phase','suite') and job.get('phase_started'):
                    elapsed=max(0,time.time()-job['phase_started'])
                    if kind=='phase' or event.get('status') in FINAL:
                        phases=job.setdefault('phase_seconds',{})
                        phases[job.get('phase','unknown')]=round(phases.get(job.get('phase','unknown'),0)+elapsed,3)
                        job.pop('phase_started',None)
                if kind == 'suite':
                    job.update({key: event[key] for key in ('status', 'exit_code', 'duration_seconds') if key in event})
                    if event.get('timing_source')=='worker_monotonic' and isinstance(event.get('phase_seconds'),dict):
                        from runtime_stats import number
                        job['phase_seconds']={k:v for k,v in event['phase_seconds'].items() if k in ('test','prepare','waiting_for_dependencies') and number(v) is not None}
                        job['timing_source']='worker_monotonic'
                    if job['status'] in FINAL:job['finished']=now()
                    if job['status'] == 'passed' and job.get('artifact_error'):
                        job['status'] = 'failed'
                    if job['status'] == 'running':
                        job['started'] = now()
                elif kind == 'phase':
                    job['phase'] = event.get('phase')
                    job['phase_started']=time.time()
                elif kind == 'failure':
                    job.setdefault('failure_details', []).append(str(event.get('line', ''))[:2000])
                    job['failure_details'] = job['failure_details'][:240]
                elif kind == 'counts':
                    job['test_counts'] = {key: value for key, value in event.get('counts', {}).items() if key in ('tests', 'passed', 'failed', 'skipped') and isinstance(value, int) and value >= 0}
                elif kind in ('artifact_begin', 'artifact_chunk', 'artifact_end') and suite in ('visual','mobile'):
                    try:
                        artifact = self.artifacts.receive(self.run['id'], host['host'], suite, event)
                        if artifact:
                            job.setdefault('artifacts', []).append(artifact)
                            job['visual_review'] = 'Human review required; rendering success is not design approval'
                    except (ValueError, OSError) as exc:
                        job['artifact_error'] = True
                        job.setdefault('failure_details', []).append('Visual artifact: ' + str(exc)[:300])
                        job['failure_details'] = job['failure_details'][:240]
                elif kind == 'log':
                    job['logs'].append(str(event.get('line', ''))[:2000])
                    job['logs'] = job['logs'][-80:]
            self.run['updated'] = now()
            if kind not in ('log', 'artifact_chunk') or time.monotonic() - self.last_save > 1:
                if persist:self.save()

    def host(self, index, host, manifest, ident):
        selected=set(manifest['hosts'][index]['suites'])
        if all(job['status']=='reused' for suite,job in host['jobs'].items() if suite in selected and not SUITES[suite].get('coordinated')):
            with self.lock:
                host.update(status='passed',finished=now());host.setdefault('duration_seconds',0)
                self.save()
            return
        from agent_client import AgentClient
        started=time.monotonic();client=AgentClient(host);cursor=0
        try:
            with self.lock:
                host.update(status='preparing',started=host.get('started') or now(),agent_job_id=ident);self.save()
            if self.run.get('stop_requested'):return
            client.call('job.start',{'id':ident,'manifest':manifest,'index':index},timeout=180)
            with self.lock:host['status']='running';self.save()
            deadline=time.monotonic()+7300
            while time.monotonic()<deadline:
                if self.run.get('stop_requested'):
                    client.call('job.stop',{'id':ident},timeout=40)
                result=client.call('job.poll',{'id':ident,'cursor':cursor})
                if result.get('events_lost'):raise RuntimeError('Agent event history has a gap; result cannot be verified')
                self.update_events(host,[{**item['event'], '_worker_sequence':item['sequence'], '_worker_job':ident} for item in result['events']])
                if result['events']:
                    with self.lock:self.save()
                previous=cursor;cursor=result['cursor']
                if result['status'] not in ('running','starting') and cursor==previous:
                    jobs=[j for suite,j in host['jobs'].items() if suite in selected and not SUITES[suite].get('coordinated')]
                    host['status']='stopped' if result['status']=='stopped' else 'passed' if result['status']=='completed' and all(j['status'] in SUCCESS for j in jobs) else 'failed'
                    if result.get('error'):host['error']=result['error']
                    break
                time.sleep(.5)
            else:
                client.call('job.stop',{'id':ident},timeout=40)
                raise RuntimeError('Agent job exceeded coordinator deadline')
        except InterruptedError:
            host['status']='stopped'
        except Exception as error:
            host.update(status='unavailable',error=type(error).__name__+': '+str(error)[:500])
            try:client.call('job.stop',{'id':ident},timeout=40)
            except Exception:pass
        finally:
            with self.lock:
                host.update(finished=now(),duration_seconds=round(host.get('duration_seconds',0)+time.monotonic()-started,2))
                for suite,job in host['jobs'].items():
                    if suite not in selected:continue
                    if SUITES[suite].get('coordinated') and not self.run.get('stop_requested'):continue
                    if job['status'] not in FINAL:job['status']='stopped' if self.run.get('stop_requested') else 'unavailable'
                self.save()

    def image_progress(self, image, phase, source, destination, progress=None):
        with self.lock:
            self.run['image_progress'] = {'image': image, 'phase': phase, 'source': source, 'destination': destination, **(progress or {})}
            self.run['updated'] = now()
            self.save()

    def execute(self):
        try:
            with self.lock:
                run = self.run
                hosts = run['hosts']
            if run.get('selection') == {'paths':[], 'suites':['mobile']}:
                from github_sources import publish,source_digest
                from mobile_worker import MobileRun
                with self.publication_lock:repositories=publish(self.root,run['id'],lambda:bool(run.get('stop_requested')),lambda name,record:None)
                with self.lock:
                    run.update(hosts=[],repositories=repositories,source_sha256=source_digest(repositories),status='running',granular=True,pending_tasks=[])
                    self.save()
                mobile=MobileRun(self,required=True);mobile.join()
                run['status']=outcome(run,[mobile.host],{})
                return
            images = ImageDistributor(hosts, no_remote_shell, lambda: bool(run.get('stop_requested')), self.image_progress)
            from agent_client import AgentSessions
            def session_progress(host, phase, detail):
                with self.lock:
                    host['session'] = {'status':phase, **(detail or {})}
                    run['preparation'] = 'Launcher session: '+host['host']+' · '+phase
                    self.save()
            sessions = AgentSessions(self.root, hosts,
                lambda:bool(run.get('stop_requested')),session_progress)
            excluded = {}
            def exclude(candidates, survivors, stage):
                names = {host['host'] for host in survivors}
                for host in candidates:
                    if host['host'] in names:continue
                    reason = host.get('error') or images.failures.get(host['host'],{}).get('reason') or 'Worker verification failed'
                    host.update(status='unavailable',excluded=True,exclusion_stage=stage,error=reason,finished=now())
                    if not run.get('targeted'):
                        host.update(jobs={},suites=[])
                    for job in host.get('jobs',{}).values():
                        job.update(status='unavailable',not_executed=True,phase=stage)
                        job.setdefault('logs',[]).append('Not executed: '+reason)
                    excluded[host['host']] = host
            def publish():
                with self.lock:
                    run['hosts'] = hosts + list(excluded.values())
                    run['verified_hosts'] = [host['host'] for host in hosts if host.get('session',{}).get('verified')]
                    run['unavailable_hosts'] = list(excluded)
                    run['excluded_hosts'] = [{'host':h['host'],'stage':h['exclusion_stage'],'reason':h['error']} for h in excluded.values()]
                    self.save()
            reachable = sessions.accessible()
            # A previous task failure must not remove an enrolled, reachable
            # machine from the fleet. Retry placement remains capacity based.
            exclude(hosts,reachable,'preflight')
            hosts = reachable
            images.hosts = hosts
            publish()
            initialized = sessions.verify(hosts) if hosts else []
            exclude(hosts,initialized,'launcher_verification')
            hosts = initialized if run.get('targeted') else plan(initialized,run.get('selection')) if initialized else []
            images.hosts = hosts
            publish()
            if not hosts:
                raise RuntimeError('No verified connected workers; see excluded machine details')
            # Publish shared immutable sources once; each worker then prepares and
            # executes independently. Never re-plan work after dispatch has begun.
            from github_sources import publish as publish_github,source_digest
            def publication_progress(name,record):
                with self.lock:
                    run['repositories'][name]=record
                    run['preparation']='GitHub: '+name+' · '+record['stage']
                    self.save()
            if run.get('pinned_repositories'):
                repositories=copy.deepcopy(run['pinned_repositories'])
            else:
                with self.publication_lock:
                    repositories=publish_github(self.root,run['id'],lambda:bool(run.get('stop_requested')),publication_progress)
            with self.lock:
                run['repositories']=repositories
                run['source_sha256']=source_digest(repositories)
                run['source_transport']='github'
                run['images']={}
                run.pop('preparation',None)
                run['status']='stopping' if run.get('stop_requested') else 'running'
                self.save()
            from distributed_images import required_images
            from suite_cache import SuiteCache
            def prepare_and_run(index,host,selected_suites=None):
                selected_suites=selected_suites or list(host['jobs'])
                try:
                    with self.lock:
                        host.pop('finished',None)
                        host.update(status='preparing',preparation='Preparing required images')
                        self.save()
                    with self.lock:
                        cache_plan=copy.deepcopy(run)
                        cache_plan['hosts'][index]['jobs']={k:v for k,v in cache_plan['hosts'][index]['jobs'].items() if k in selected_suites}
                    cache=SuiteCache(self.root,self.folder,SUITES)
                    if not run.get('targeted') and not run.get('force'):cache.apply(cache_plan,images,{host['host']})
                    candidate=next(row for row in cache_plan['hosts'] if row['host']==host['host'])
                    needed=list(dict.fromkeys(SUITES[suite]['image'] for suite,job in candidate['jobs'].items() if job['status']!='reused'))
                    if any(job['status']!='reused' and not SUITES[suite].get('coordinated') for suite,job in candidate['jobs'].items()):
                        from distributed_worker import PYTHON
                        if PYTHON not in needed:needed.append(PYTHON)
                    for image in needed:
                        if run.get('stop_requested'):return
                        images.ensure(image,[host])
                    verified=sessions.verify([host])
                    if not verified:raise RuntimeError(host.get('error') or 'Worker readiness verification failed')
                    with self.lock:
                        host['images']=dict(images.host_images.get(host['host'],{}))
                        run['images'].update(images.images)
                        cache_plan=copy.deepcopy(run)
                        cache_plan['hosts'][index]['jobs']={k:v for k,v in cache_plan['hosts'][index]['jobs'].items() if k in selected_suites}
                    if not run.get('targeted') and not run.get('force'):cache.apply(cache_plan,images,{host['host']})
                    with self.lock:
                        cached=next(row for row in cache_plan['hosts'] if row['host']==host['host'])
                        host['jobs'].update(cached['jobs'])
                        host.pop('preparation',None)
                        run['reuse']={'suites':sum(j['status']=='reused' for h in run['hosts'] for j in h['jobs'].values()),
                            'policy':'relevant-source+dependencies+image+machine','fresh_passes_only':True}
                        manifest=copy.deepcopy(run)
                        manifest['hosts'][index]['suites']=selected_suites
                        manifest['id']=run['id'].split('-')[0]+'-'+uuid.uuid4().hex[:8]
                        self.save()
                    if not run.get('stop_requested'):self.host(index,host,manifest,manifest['id'])
                except Exception as error:
                    with self.lock:
                        host.update(status='unavailable',error=type(error).__name__+': '+str(error)[:500],finished=now())
                        for job in host['jobs'].values():
                            if job['status'] not in FINAL:
                                job.update(status='unavailable',not_executed=True,phase='image_preparation')
                        self.save()
            if run.get('targeted'):
                from work_queue import catalog
                catalog(self.root, SUITES, run['repositories'])
                with concurrent.futures.ThreadPoolExecutor(max_workers=len(hosts)) as pool:
                    futures=[pool.submit(prepare_and_run,index,host) for index,host in enumerate(hosts)]
                    for future in concurrent.futures.as_completed(futures):future.result()
            else:
                from granular_dispatch import execute
                execute(self,hosts,images)
            if not run.get('stop_requested') and not run.get('p2p_early'):
                if not run.get('targeted') and not run.get('force'):
                    with self.lock:
                        peer_plan=copy.deepcopy(run)
                        for row in peer_plan['hosts']:
                            row['jobs']={key:job for key,job in row['jobs'].items() if key=='p2p'}
                    SuiteCache(self.root,self.folder,SUITES).apply(peer_plan,images)
                    with self.lock:
                        for row in peer_plan['hosts']:
                            original=next(h for h in run['hosts'] if h['host']==row['host'])
                            if 'p2p' in row['jobs']:original['jobs']['p2p']=row['jobs']['p2p']
                # P2P alone needs the participant set; ordinary suites have already
                # completed on each ready worker without waiting for slower nodes.
                with self.lock:
                    peers=[h['jobs']['p2p'] for h in hosts if h['status']!='unavailable' and 'p2p' in h['jobs']]
                    if peers and not all(j['status']=='reused' for j in peers):
                        for job in peers:
                            if job['status']=='reused':
                                job.update(status='queued',phase=None,not_executed=False,logs=[],duration_seconds=None)
                                job.pop('reused_from',None)
                    run.setdefault('reuse',{})['suites']=sum(j['status']=='reused' for h in run['hosts'] for j in h['jobs'].values())
                    self.save()
                if any('p2p' in h['jobs'] for h in hosts):self.network(run,images)
            with self.lock:
                run['status'] = outcome(run, hosts+[h for h in run['hosts'] if h.get('kind')=='android'], excluded)
        except Exception as exc:
            with self.lock:
                self.run.update(status='failed', error=type(exc).__name__ + ': ' + str(exc)[:500])
        finally:
            with self.lock:
                if self.run.get('stop_requested'):
                    self.run['status'] = 'stopped'
                    for host in self.run['hosts']:
                        if host['status'] not in FINAL:
                            host['status'] = 'stopped'
                        for job in host['jobs'].values():
                            if job['status'] not in FINAL:
                                job['status'] = 'stopped'
                elif self.run.get('status') == 'failed':
                    for host in self.run['hosts']:
                        if host['status'] not in FINAL:
                            host['status'] = 'failed'
                        for job in host['jobs'].values():
                            if job['status'] not in FINAL:
                                job.update(status='failed',phase='infrastructure')
                self.running = False
                self.run['finished'] = now()
                self.save()
                if self.run['status']=='passed' and not self.run.get('targeted') and not self.run.get('selection'):
                    completed=self.run
                    def post_deployment():
                        from post_deployment import execute
                        def persist():
                            with self.lock:
                                path=self.folder/(completed['id']+'.json')
                                temporary=path.with_suffix('.post.tmp')
                                temporary.write_text(json.dumps(completed));temporary.chmod(0o600);temporary.replace(path)
                        execute(completed,persist)
                    threading.Thread(target=post_deployment,daemon=True).start()


    def network(self,run,images=None,parallel=False):
        from p2p_network import NetworkP2P
        hosts=[host for host in run['hosts'] if not host.get('excluded') and host.get('session',{}).get('verified')
               and host.get('status')!='unavailable' and 'p2p' in host.get('jobs',{})]
        if hosts and all(host['jobs']['p2p']['status']=='reused' for host in hosts):
            with self.lock:
                run['p2p']={'status':'reused','not_executed':True,'reason':'Unchanged code, images and participant identities; see each suite baseline'}
                self.save()
            return
        if len(hosts)<2:
            with self.lock:
                reason='At least two verified connected machines required; network coverage not measured'
                run['p2p']={'status':'unavailable','error':reason,'checks':[]}
                for host in hosts:
                    host['jobs']['p2p'].update(status='unavailable',not_executed=True,phase='coverage')
                    host['jobs']['p2p']['logs'].append(reason)
                    if not parallel:host['status']='partial' if all(j['status'] in SUCCESS for key,j in host['jobs'].items() if key!='p2p') else 'failed'
                self.save()
            return
        started=time.monotonic()
        with self.lock:
            for host in hosts:
                host['jobs']['p2p'].update(status='running',phase='network',started=now())
                if not parallel:host['status']='running'
            self.save()
        result=NetworkP2P(hosts,run['id'],emit=self.update,
                          stopped=lambda:bool(run.get('stop_requested')),source_commit=run['repositories']['.']['commit'],images=images,
                          source_repositories=run['repositories']).run()
        with self.lock:
            run['p2p']=result
            for host in hosts:
                job=host['jobs']['p2p'];job.update(status=result['status'],duration_seconds=round(time.monotonic()-started,2),
                    test_counts={'tests':4,'passed':sum(c['status']=='passed' for c in result['checks']),
                                 'failed':0 if result['status']=='passed' else 1,'skipped':0})
                job['logs'].append('Direct network coverage: '+result['coverage']['direct']+'; cross-NAT: '+result['coverage']['cross_nat']+'; TURN: '+result['coverage']['turn'])
                if result.get('error'):job.setdefault('failure_details',[]).append(result['error'])
                for check in result['checks']:
                    routes=list(check.get('routes',[]))
                    for receiver in check.get('receivers',[]):routes.extend(receiver['routes'])
                    for route in routes:
                        pair=route['pair'];job['logs'].append(check['name']+': '+json.dumps(pair,separators=(',',':')))
                if not parallel:
                    host['status']='passed' if all(j['status'] in SUCCESS for j in host['jobs'].values()) else 'failed'
                    host['finished']=now()
            self.save()
