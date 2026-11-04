from pathlib import Path
"""Dispatch file jobs to whichever persistent worker has CPU and memory free."""
import concurrent.futures
import time
import uuid
import http.client

from agent_client import AgentClient, wait_for_events
from distributed_worker import SUITES, PYTHON
from suite_cache import SuiteCache
from work_queue import catalog, pending, claim
from submission_queue import SubmissionQueue
from worker_preparation import execution_capacity
from early_p2p import EarlyP2P


def recover_transport(operation, stopped, deadline, report):
    """Keep accepted work and its cursor during transport loss; never restart it."""
    reconnecting=False
    while time.monotonic()<deadline:
        if stopped():raise InterruptedError('Worker recovery stopped')
        try:
            result=operation()
            if reconnecting:report(False)
            return result
        except (PermissionError,FileNotFoundError):raise
        except (OSError,http.client.HTTPException):
            if not reconnecting:report(True)
            reconnecting=True;time.sleep(1)
    raise TimeoutError('Worker session deadline exceeded during reconnection')


def focused_workers(hosts,tasks,specs):
    """Choose from current capacity and cached images, rather than host order."""
    memory=max(specs[t].get('memory_mb',1536) for t in tasks)+512
    cpus=max(specs[t].get('cpus',1) for t in tasks)+1
    images={PYTHON,*[specs[t]['image'] for t in tasks]}
    def probe(item):
        index,host=item;client=AgentClient(host)
        try:
            available=client.call('resource.capacity',timeout=10)
            host['admission_capacity']=available
            if available.get('available_mb',0)<memory or available.get('available_cpus',0)<cpus:return None
            cached=0
            for image in images:
                try:cached+=bool(client.call('image.inspect',{'image':image},timeout=10))
                except (OSError,RuntimeError):pass
            return (-cached,index,host)
        except (OSError,RuntimeError):return None
        finally:client.close()
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(5,len(hosts))) as pool:
        ready=[row for row in pool.map(probe,enumerate(hosts)) if row is not None]
    # If nobody currently fits, retain independent workers rather than pinning
    # the queue to one full or unreachable machine until its session expires.
    return [row[2] for row in sorted(ready,key=lambda r:r[:2])[:len(tasks)]] if ready else hosts


def execute(service, hosts, images):
    SUITES = __import__('copy').deepcopy(__import__('distributed_worker').SUITES)
    run = service.run
    tasks = catalog(service.root, SUITES, run['repositories'])
    from test_scope import select
    tasks = select(tasks, SUITES, run.get('selection'))
    include_p2p = not run.get('selection') or 'p2p' in run['selection']['suites']
    if run.get('selection') and run['selection']['suites'] == ['p2p'] and not run['selection']['paths']:
        tasks = []
    with service.lock:
        run.update(granular=True, task_catalog=tasks, pending_tasks=pending(service.folder, tasks))
        for host in hosts:
            host['suites'] = ['p2p'] if include_p2p else []
            host['jobs'] = {'p2p': {'name': SUITES['p2p']['name'], 'status': 'queued', 'phase': None, 'logs': []}} if include_p2p else {}
            host.pop('org_api_shard', None)
            host['active_tasks'] = []
        service.save()

    submissions=SubmissionQueue('application-tests:'+run['id'])
    for task in tasks:
        spec=SUITES[task]
        submissions.submit({'id':task,'kind':'application-test','command':spec['test'],
            'repositories':run['repositories'],'resources':{'cpus':spec.get('cpus',1),'memory_mb':spec.get('memory_mb',1536)}})
    queue_tokens={}

    preparation_bases=list(dict.fromkeys(SUITES[t].get('base',t) for t in run['pending_tasks']))
    network=EarlyP2P(service,hosts,images,include_p2p)
    from gpu_worker import GpuRun
    gpu=GpuRun(service,hosts,images,not run.get('selection') or 'gpu' in run['selection']['suites'])
    from mobile_worker import MobileRun
    mobile=MobileRun(service,required=True) if run.get('selection') and 'mobile' in run['selection']['suites'] else MobileRun(service) if not run.get('selection') else None

    if not tasks:
        network.join()
        gpu.join()
        if mobile:mobile.join()
        with service.lock:
            for host in hosts:host['status']='passed' if all(j['status'] in ('passed','reused') for j in host['jobs'].values()) else 'failed'
            service.save()
        return

    if run.get('selection') and not include_p2p and not gpu.hosts and len(hosts) > len(tasks):
        chosen=focused_workers(hosts,tasks,SUITES)
        for host in [h for h in hosts if h not in chosen]:
            host.update(status='passed',not_executed=True,preparation='No additional worker needed for this focused request')
        hosts=chosen

    def worker(index, host):
        client = AgentClient(host)
        ident = run['id'].split('-')[0] + '-' + uuid.uuid4().hex[:8]
        active = set()
        cursor = 0
        closed = False
        close_sent = False
        ready = False
        ready_bases=set()
        preparing=True
        p2p_released=False
        started = time.monotonic()
        cache = SuiteCache(service.root, service.folder, SUITES)
        baselines = cache.baselines(run)
        def finish_unused(stop=False):
            with service.lock:
                unused=not include_p2p and not run.get('stop_requested') and not run['pending_tasks'] and not active and not host['jobs']
            if not unused:return False
            if stop:client.call('job.stop',{'id':ident},timeout=40)
            with service.lock:
                host.update(status='passed',not_executed=True,preparation='All selected tests were claimed by other workers')
                service.save()
            return True
        try:
            if finish_unused():return
            with service.lock:
                host.update(status='preparing', preparation='Preparing persistent file worker')
                service.save()
            # Machines initialize independently. No fleet-wide image/setup barrier.
            bases = preparation_bases
            for image in dict.fromkeys([PYTHON] + [SUITES[b]['image'] for b in bases]):
                if run.get('stop_requested'):return
                if finish_unused():return
                images.ensure(image, [host])
            if finish_unused():return
            status=client.call('status')
            if not status.get('ready') or status.get('machine')!=host['session']['machine'] or status.get('source_revision')!=host['agent_status']['source_revision']:
                raise RuntimeError('Worker readiness or identity changed during preparation')
            with service.lock:
                host['images'] = dict(images.host_images.get(host['host'], {}))
                run['images'].update(images.images)
                host.update(agent_job_id=ident, status='running')
                host.pop('preparation', None)
                # Keep the agent request bounded even if another worker already
                # produced thousands of log lines while this machine initialized.
                manifest = {'id':ident,'granular':True,'task_catalog':tasks,'p2p_parallel':not network.done.is_set(),
                            'source_sha256':run['source_sha256'],
                            'repositories':{name:{key:repo[key] for key in ('url','commit')} for name,repo in run['repositories'].items()},
                            'hosts':[{'host':h['host'],'suites':bases if h is host else [],
                                      'images':host['images'] if h is host else {},'jobs':{}} for h in hosts]}
                service.save()
            # Inspect once per image outside the global queue lock, not once per file.
            for image in dict.fromkeys(SUITES[b]['image'] for b in bases):
                cache.image_fingerprints[(host['session']['machine'],image,host['images'].get(image))] = images.fingerprint(host,image)
            if run.get('stop_requested'):return
            client.call('job.start', {'id': ident, 'manifest': manifest, 'index': index}, timeout=180)
            deadline = time.monotonic() + 7300
            def connection_state(disconnected):
                with service.lock:
                    host['connection_status']='reconnecting' if disconnected else 'connected'
                    if disconnected:host['preparation']='Connection interrupted; accepted jobs continue; reconnecting to saved events'
                    elif host.get('preparation','').startswith('Connection interrupted;'):host.pop('preparation',None)
                    service.save()
            def recover(operation):
                return recover_transport(operation,lambda:bool(run.get('stop_requested')),deadline,connection_state)
            while time.monotonic() < deadline:
                if run.get('stop_requested'):
                    client.call('job.stop', {'id': ident}, timeout=40)
                    break
                result = recover(lambda:wait_for_events(client,ident,cursor,lambda:bool(run.get('stop_requested'))))
                changed = False
                if result.get('events_lost'):
                    raise RuntimeError('Worker event history has a gap')
                events = [{**item['event'], '_worker_sequence':item['sequence'], '_worker_job':ident} for item in result['events']]
                service.update_events(host,events)
                for event in events:
                    if event.get('kind') == 'capacity':
                        with service.lock:
                            host['capacity'] = {k: int(event[k]) for k in ('cpus', 'memory_mb')}
                            ready = True
                            changed = True
                    else:
                        if event.get('kind')=='dependencies':
                            if event.get('status') in ('ready','failed'):ready_bases.add(event['base'])
                            if event.get('status')=='complete':preparing=False
                        changed=True
                    if event.get('kind') == 'suite' and event.get('status') in ('passed', 'failed', 'timed_out'):
                        active.discard(event['suite'])
                        if event['suite'] in queue_tokens:
                            submissions.update(event['suite'],queue_tokens[event['suite']],state='completed' if event['status']=='passed' else 'failed',result=__import__('json').dumps(event))
                previous, cursor = cursor, result['cursor']
                if result['status'] not in ('running', 'starting'):
                    if cursor != previous:continue  # Drain final events without feeding an exited worker.
                    if result['status'] != 'completed' or active:
                        raise RuntimeError(result.get('error') or ('Worker ended before its tasks completed: '+''.join(host.get('logs',[])[-3:])))
                    break
                if finish_unused(stop=True):return
                outgoing = []
                policy=recover(lambda:client.call('worker.policy'))
                host['worker_policy']=policy
                if not policy['accepting_jobs']:
                    closed=True
                    host['preparation']=policy['reason']+'; finishing accepted jobs'
                release_p2p=network.done.is_set() and not p2p_released
                with service.lock:
                    if ready and not closed:
                        budget=execution_capacity(host['capacity'],preparing=preparing,p2p=not network.done.is_set())
                        host['execution_capacity']=budget
                        while len(outgoing)<128:
                            available={'available_cpus':budget['cpus']-sum(SUITES[t].get('cpus',1) for t in active),
                                'available_mb':budget['memory_mb']-sum(SUITES[t].get('memory_mb',1536) for t in active)}
                            selected=submissions.claim(host['host'],available,eligible=[t for t in run['pending_tasks'] if SUITES[t].get('base',t) in ready_bases])
                            if selected is None:break
                            task=selected['id'];run['pending_tasks'].remove(task);queue_tokens[task]=selected['token']
                            submissions.update(task,selected['token'],state='accepted',task_id=ident)
                            changed = True
                            job = {'name': SUITES[task]['name'], 'suite': SUITES[task].get('base', task),
                                   'file': SUITES[task].get('file'), 'status': 'queued', 'phase': None, 'logs': []}
                            job.update(cpus=SUITES[task].get('cpus',1),memory_mb=SUITES[task].get('memory_mb',1536))
                            host['suites'].append(task)
                            host['jobs'][task] = job
                            candidate = {**run, 'hosts': [{**host, 'jobs': {task: job}}]}
                            cache.apply(candidate, images, baselines=baselines, force=run.get('force',False))
                            if job['status'] == 'reused':
                                submissions.update(task,queue_tokens[task],state='completed',result=__import__('json').dumps({'reused':True}))
                                continue
                            active.add(task)
                            outgoing.append(task)
                        changed = changed or host['active_tasks'] != sorted(active)
                        host['active_tasks'] = sorted(active)
                        # Once all work is claimed, slow workers finish their own files;
                        # faster workers are never forced to repeat a suite or wait idle.
                        closed = not run['pending_tasks'] and not active
                        available = [h for h in hosts if h.get('capacity') and h['status'] != 'unavailable']
                        if (run['pending_tasks'] and len(available) == len([h for h in hosts if h['status'] != 'unavailable'])
                                and not any(claim(list(run['pending_tasks']),SUITES,h['capacity'],set()) is not None for h in available)):
                            raise RuntimeError('Remaining tasks exceed every available worker CPU/memory budget')
                        if changed or (closed and not close_sent):service.save()
                if outgoing or (closed and not close_sent) or release_p2p:
                    p2p_finished=network.done.is_set()
                    try:
                        # Same run ID and task markers make feed acknowledgement
                        # retries idempotent, including an acknowledgement lost in transit.
                        recover(lambda:client.call('job.feed', {'id': ident, 'tasks': outgoing, 'close': closed,'p2p_finished':p2p_finished}))
                    except RuntimeError:
                        policy=client.call('worker.policy')
                        if policy['accepting_jobs'] or not outgoing:raise
                        # The agent rejected the whole feed before accepting files.
                        # Preserve already running jobs and return unaccepted work.
                        with service.lock:
                            for task in outgoing:
                                submissions.update(task,queue_tokens[task],state='queued',host=None,task_id=None)
                                run['pending_tasks'].append(task);active.discard(task)
                                host['jobs'].pop(task,None);host['suites'].remove(task)
                            host['worker_policy']=policy;service.save()
                        closed=True
                        client.call('job.feed', {'id':ident,'tasks':[],'close':True,'p2p_finished':p2p_finished})
                    p2p_released=p2p_finished
                    close_sent = closed
            else:
                raise RuntimeError('Persistent worker exceeded its deadline')
            with service.lock:
                host['status'] = 'stopped' if run.get('stop_requested') else 'passed' if all(j['status'] in ('passed', 'reused') for t, j in host['jobs'].items() if t != 'p2p') else 'failed'
        except Exception as error:
            try:client.call('job.stop', {'id': ident}, timeout=40)
            except Exception:pass
            with service.lock:
                host.update(status='unavailable', error=type(error).__name__ + ': ' + str(error)[:500])
                for task in active:
                    host['jobs'][task].update(status='unavailable', phase='worker_lost')
        finally:
            client.close()
            with service.lock:
                from distributed import now
                host.update(active_tasks=[], finished=now(), duration_seconds=round(time.monotonic() - started, 2))
                service.save()

    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(hosts)) as pool:
            futures = [pool.submit(worker, index, host) for index, host in enumerate(hosts)]
            for future in concurrent.futures.as_completed(futures):future.result()
    finally:
        network.join()
        gpu.join()
        if mobile:mobile.join()
    if run['pending_tasks'] and not run.get('stop_requested'):
        raise RuntimeError('No worker could execute the remaining ' + str(len(run['pending_tasks'])) + ' test jobs')
