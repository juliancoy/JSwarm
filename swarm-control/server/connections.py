"""Independent authenticated peer heartbeats; slow hosts never delay healthy peers."""
from concurrent.futures import ThreadPoolExecutor
import json
import threading
import time
from agent_client import AgentClient, CONFIG, PROTOCOL

INTERVAL=1.0
TIMEOUT=2.0
STALE_AFTER=5.0
MAX_HOSTS=64


class Connections:
    def __init__(self, config=CONFIG, probe=None, clock=time.time):
        self.config=config;self.clock=clock;self.probe=probe or self.heartbeat
        self.lock=threading.Lock();self.rows={};self.error=None;self.enrolled=None

    @staticmethod
    def heartbeat(host,address):
        client=AgentClient({'host':host,'agent_endpoint':'http://'+address+':8790'})
        try:
            result=client.call('status',timeout=TIMEOUT)
            if result.get('protocol')!=PROTOCOL or result.get('node')!=host or not result.get('machine'):
                raise ValueError('Peer identity mismatch')
            policy=result.get('worker_policy',{})
            return {'ready':result.get('ready') is True,'accepting_jobs':policy.get('accepting_jobs') is True}
        finally:client.close()

    def inventory(self):
        config=json.loads(self.config.read_text())
        peers=config['peers']
        if not isinstance(peers,dict) or len(peers)>MAX_HOSTS:raise ValueError('Invalid peer inventory')
        return {host:address for host,address in peers.items() if isinstance(host,str) and isinstance(address,str)}

    def check(self,host,address):
        now=self.clock()
        with self.lock:
            previous=dict(self.rows.get(host,{}))
        row={'host':host,'address':address,'checked_at':now,'last_seen':previous.get('last_seen'),'connected_at':previous.get('connected_at')}
        try:
            detail=self.probe(host,address)
            row.update(detail,status='connected',last_seen=self.clock(),checked_at=self.clock())
            if previous.get('status')!='connected' or previous.get('address')!=address:row['connected_at']=row['last_seen']
        except (OSError,ValueError,RuntimeError,KeyError,TypeError) as error:
            denied=isinstance(error,PermissionError) or str(error) in ('Agent HTTP status 401','Agent HTTP status 403')
            row.update(status='authentication_denied' if denied else 'disconnected',ready=False,accepting_jobs=False,checked_at=self.clock(),connected_at=None)
        with self.lock:
            if self.enrolled is None or self.enrolled.get(host)==address:self.rows[host]=row

    def view(self):
        now=self.clock()
        with self.lock:
            rows=[dict(row) for row in self.rows.values()];error=self.error
        for row in rows:
            if error is not None or now-row['checked_at']>STALE_AFTER:
                row.update(status='unknown',ready=False,accepting_jobs=False)
        rows.sort(key=lambda row:row['host'])
        return {'hosts':rows,'sampled_at':now,'poll_interval_seconds':INTERVAL,'heartbeat_timeout_seconds':TIMEOUT,'stale_after_seconds':STALE_AFTER,'inventory_available':error is None}

    def worker(self,stop=None):
        stop=stop or threading.Event();pending={};due={}
        with ThreadPoolExecutor(max_workers=MAX_HOSTS) as pool:
            while not stop.is_set():
                try:
                    peers=self.inventory()
                    with self.lock:
                        self.error=None;self.enrolled=dict(peers)
                        for host in list(self.rows):
                            if host not in peers:self.rows.pop(host)
                        for host,address in peers.items():
                            if host not in self.rows or self.rows[host]['address']!=address:self.rows[host]={'host':host,'address':address,'status':'unknown','checked_at':0,'last_seen':None,'connected_at':None}
                except (OSError,ValueError,KeyError,TypeError):
                    peers={}
                    with self.lock:self.error='Peer inventory unavailable'
                for host,future in list(pending.items()):
                    if future.done():
                        # check() contains probe failures; unexpected failures are retried.
                        future.exception();pending.pop(host)
                now=time.monotonic()
                for host,address in peers.items():
                    if host not in pending and now>=due.get(host,0):
                        pending[host]=pool.submit(self.check,host,address);due[host]=now+INTERVAL
                stop.wait(.1)
