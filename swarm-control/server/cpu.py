"""Live host CPU, memory, disk I/O, and network samples without installing agents."""
import concurrent.futures
import json
import subprocess
import threading
import time
import sys
import math
from interface_metrics import InterfaceSampler
from disk_metrics import DiskSampler
from resource_history import ResourceHistory
from gpu_metrics import GpuSampler
from paths import WORKSPACE
from pathlib import Path
PINNED_SCRIPTS=(Path(__file__).resolve().parents[3] if Path(__file__).resolve().parents[2].name == 'JSwarm' else Path(__file__).resolve().parents[2])/'scripts'
sys.path.insert(0,str(PINNED_SCRIPTS if PINNED_SCRIPTS.is_dir() else WORKSPACE/'scripts'))
from launcher_agent import hardware_sample, ResourceSampler, PerformanceCollector
from distributed import targets
from agent_client import AgentClient, CONFIG


def aggregate(hosts):
    fresh=[h for h in hosts if isinstance(h.get('percent'),(int,float)) and math.isfinite(h['percent'])
           and 0<=h['percent']<=100 and isinstance(h.get('cpus'),int) and h['cpus']>0 and h.get('status')=='live']
    capacity=sum(h['cpus'] for h in fresh)
    return round(sum(h['percent']*h['cpus'] for h in fresh)/capacity,1) if capacity else None


class CpuMonitor:
    def __init__(self, activity=None, network=None):
        self.lock=threading.Lock()
        self.hosts=[]
        self.history=ResourceHistory()
        self.interfaces={}
        self.disks={}
        self.gpu_samplers={}
        self.sampler=ResourceSampler()
        self.performance=PerformanceCollector()
        self.activity=activity
        self.network=network
        self.telemetry_policy=None

    def sample(self, target):
        local=target.get('local',False)
        try:
            if local:
                values=self.sampler.sample()
                values['performance']=self.performance.sample()
                hardware=hardware_sample(disk=WORKSPACE)
                try:
                    config=json.loads(CONFIG.read_text())
                    client=AgentClient({'host':config['name'],'agent_endpoint':'http://'+config['peers'][config['name']]+':8790'})
                    try:detail=client.call('status',timeout=3)
                    finally:client.close()
                    hardware={**detail.get('hardware',{}),**hardware}
                except (OSError,ValueError,RuntimeError,KeyError):pass
                values['hardware']=hardware
            else:
                config=json.loads(CONFIG.read_text())
                address=config['peers'][target['host']]
                client=AgentClient({**target,'agent_endpoint':'http://'+address+':8790'})
                try:values=client.call('metrics',timeout=8)
                finally:client.close()
            if values.get('telemetry_transport')=='launcher-push':return {'host':target['host'],**values}
            sampler=self.interfaces.setdefault(target['host'],InterfaceSampler())
            values['network_interfaces']=sampler.sample(target)
            values['disks']=self.disks.setdefault(target['host'],DiskSampler()).sample(target)
            gpu_sampler=self.gpu_samplers.setdefault(target['host'],GpuSampler())
            values['hardware']=gpu_sampler.sample(target,values.get('hardware',{}))
            if values.get('measurement_version')!=2:
                return {'host':target['host'],'percent':None,'cpus':values.get('cpus'),'status':'update_required','sampled_at':time.time()}
            return {'host':target['host'],**values}
        except (OSError,ValueError,TypeError,RuntimeError,KeyError,subprocess.SubprocessError) as error:
            status='permission_denied' if isinstance(error,PermissionError) else 'authentication_denied' if str(error) in ('Agent HTTP status 401','Agent HTTP status 403') else 'unavailable'
            return {'host':target['host'],'percent':None,'cpus':None,'status':status,'sampled_at':time.time()}

    def view(self):
        with self.lock:
            hosts=[dict(h) for h in self.hosts]
            history=self.history.view(time.time())
        activity={};activity_status='available' if self.activity else 'unavailable'
        if self.activity:
            try:
                primary=self.activity()
                reports=[primary]+[self.activity(ident) for ident in primary.get('active_reruns',[])]
                for report in reports:
                    if not report.get('running'):continue
                    for host in (report.get('run') or {}).get('hosts',[]):
                        jobs=list(host.get('jobs',{}).values())
                        executing=[j for j in jobs if j.get('status')=='running' and j.get('phase') not in ('prepare','waiting_for_dependencies','waiting_for_capacity')]
                        entry=activity.setdefault(host['host'],{'running_test_jobs':0,'scheduled_cpu_slots':0,'workload_phase':'idle'})
                        entry['running_test_jobs']+=len(executing)
                        entry['scheduled_cpu_slots']+=sum(j.get('cpus') or 0 for j in jobs if j.get('status') in ('queued','running'))
                        phase='testing' if executing else 'preparing' if host.get('status')=='preparing' or any(v.get('status')=='preparing' for v in host.get('dependency_preparation',{}).values()) else 'waiting' if host.get('active_tasks') else 'idle'
                        if phase=='testing' or entry['workload_phase']!='testing':entry['workload_phase']=phase
            except (OSError,ValueError,TypeError,KeyError,RuntimeError):activity={};activity_status='unavailable'
        now=time.time()
        for host in hosts:
            host['sample_age_seconds']=round(max(0,now-host['sampled_at']),1)
            host.update(activity.get(host['host'],{'running_test_jobs':0,'scheduled_cpu_slots':0,'workload_phase':'idle' if activity_status=='available' else 'unknown'}))
            if host['sample_age_seconds']>15:
                host.update(status='stale',percent=None,logical_cpus=[],ram_percent=None,disk_busy_percent=None,network_mbps=None)
            if host.get('performance'):
                host['performance']={**host['performance'],'stale':now-host['performance'].get('sampled_at',0)>15}
            if host.get('hardware'):
                host['hardware']={**host['hardware'],'stale':now-host['hardware'].get('sampled_at',host['sampled_at'])>15}
        live=[h for h in hosts if h['status']=='live' and h.get('percent') is not None]
        active=[h for h in hosts if h['running_test_jobs']>0] if activity_status=='available' else []
        return {'telemetry_policy':self.telemetry_policy,'hosts':hosts,'history':history,'history_window_seconds':300,'total_percent':aggregate(hosts),'live_hosts':len(live),
                'measured_logical_cpus':sum(h['cpus'] for h in live),
                'active_test_percent':aggregate(active),'test_hosts':len(active),'measured_test_hosts':sum(h['status']=='live' for h in active),
                'activity_status':activity_status,
                'total_hosts':len(hosts),'sample_interval_seconds':5,
                'sampling_method':'Counter deltas across the full interval; first sample and counter resets warm up without reporting zero.',
                'scope':'Whole-machine executing CPU time, weighted by logical CPU count. I/O wait and stolen time are separate. Test-active machines include all their processes, not only tests. Unavailable and stale hosts are excluded.'}

    def set_telemetry_transport(self, transport):
        if transport not in ('tcp','webrtc'):raise ValueError('Choose TCP or WebRTC')
        config=json.loads(CONFIG.read_text());name=config['name']
        client=AgentClient({'host':name,'agent_endpoint':'http://'+config['peers'][name]+':8790'})
        try:policy=client.call('metrics.transport',{'transport':transport},timeout=5)
        finally:client.close()
        with self.lock:self.telemetry_policy=policy
        return policy

    def worker(self):
        # Push updates and legacy polls progress independently. A slow machine
        # cannot hold back another machine's measurements.
        pending={};attempted={};fleet_cursor={};last_inventory=0;hosts=[]
        with concurrent.futures.ThreadPoolExecutor(max_workers=9) as pool:
            while True:
                now=time.time()
                if now-last_inventory>=30:
                    try:inventory=targets()
                    except (OSError,ValueError):inventory=[]
                    hosts=[{'host':'compai.local','local':True}]+[h for h in inventory if h['host']!='compai.local']
                    last_inventory=now
                try:
                    config=json.loads(CONFIG.read_text());name=config['name']
                    client=AgentClient({'host':name,'agent_endpoint':'http://'+config['peers'][name]+':8790'})
                    try:fleet=client.call('metrics.fleet',fleet_cursor,timeout=1)
                    finally:client.close()
                    fleet_cursor={'session':fleet['session'],'after':fleet['revision']}
                    with self.lock:
                        policy=fleet.get('policy')
                        if policy and policy.get('updated_at',0)>=(self.telemetry_policy or {}).get('updated_at',0):self.telemetry_policy=policy
                        current={h['host']:h for h in self.hosts}
                        for host in fleet['hosts']:
                            if host['sampled_at']>=current.get(host['host'],{}).get('sampled_at',0):current[host['host']]=host
                        self.hosts=list(current.values())
                        for frame in fleet['events']:self.history.append(frame['hosts'],frame['sampled_at'],partial=True)
                except (OSError,ValueError,TypeError,RuntimeError,KeyError):pass
                for name,future in list(pending.items()):
                    if not future.done():continue
                    del pending[name]
                    try:sample=future.result()
                    except Exception:continue
                    with self.lock:
                        current={h['host']:h for h in self.hosts}
                        previous=current.get(name,{})
                        if previous and sample.get('status') in ('unavailable','authentication_denied','permission_denied'):continue
                        if sample['sampled_at']>previous.get('sampled_at',0):
                            current[name]=sample;self.hosts=list(current.values())
                            self.history.append([sample],sample['sampled_at'],partial=True)
                with self.lock:current={h['host']:h for h in self.hosts}
                for target in hosts:
                    name=target['host'];previous=current.get(name,{})
                    if previous.get('telemetry_transport')=='launcher-push' and now-previous.get('sampled_at',0)<15:continue
                    if name not in pending and now-attempted.get(name,0)>=5:
                        attempted[name]=now;pending[name]=pool.submit(self.sample,target)
                if self.network:
                    try:network=self.network()
                    except (OSError,ValueError,RuntimeError):network=None
                    if network is not None:
                        with self.lock:self.history.append([],time.time(),network)
                time.sleep(1)
