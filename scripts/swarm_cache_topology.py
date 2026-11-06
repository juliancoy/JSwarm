"""Advisory cache locality from enrolled nodes; admission and seals stay authoritative."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import threading
import time

from scripts.swarm_runtime import AgentClient
from scripts.swarm_shared_cache import contract,validate_binding


class CacheTopology:
    def __init__(self, hosts, *, clock=time.monotonic, client=AgentClient, refresh_seconds=60, max_age=180):
        self.hosts=dict(hosts);self.clock=clock;self.client=client
        self.refresh_seconds=refresh_seconds;self.max_age=max_age
        self.pool=ThreadPoolExecutor(max_workers=max(1,min(5,len(hosts))),thread_name_prefix='cache-topology')
        self.pending={};self.observations={};self.attempted={};self.transfers={};self.cooldown={}
        self.guard=threading.Lock()

    def _inventory(self, host):
        client=self.client(host)
        try:
            status=client.call('status',timeout=10)
            if status.get('node')!=host['host'] or status.get('machine')!=host['machine']:
                raise ValueError('Cache peer identity changed')
            inventory=client.call('resource.caches',timeout=120)
            rows=[]
            for cache in inventory['caches']:
                key,_=contract(cache['image'],cache['files'])
                if cache.get('layout')=='node-cache-v1':validate_binding(cache)
                elif cache.get('key')!=key or cache.get('container')!='swarm-job-'+key[:32]:raise ValueError('Cache inventory binding differs')
                rows.append(cache)
            return self.clock(),rows
        finally:client.close()

    def refresh(self):
        """Never wait for an inventory in the scheduling/dispatch thread."""
        with self.guard:
            for name,host in self.hosts.items():
                future=self.pending.get(name)
                if future and future.done():
                    try:self.observations[name]=future.result()
                    except Exception:self.observations.pop(name,None)
                    del self.pending[name]
                    self.attempted[name]=self.clock()
                if name not in self.pending and self.clock()-self.attempted.get(name,float('-inf'))>=self.refresh_seconds:
                    self.attempted[name]=self.clock()
                    self.pending[name]=self.pool.submit(self._inventory,host)

    def entries(self, name):
        with self.guard:
            stamp,rows=self.observations.get(name,(float('-inf'),[]))
            return list(rows) if 0<=self.clock()-stamp<=self.max_age else []

    def local_bytes(self, host, requirements):
        # Count an object set only when it is available in a single sealed owner.
        # Splitting files across unrelated owners would not provide a usable mount.
        sets=[c['files'] for c in self.entries(host['host'])]
        return sum(sum(r['bytes'] for r in files.values()) for files in requirements
                   if files in sets)

    def peers(self, destination, files, *, exclude=()):
        candidates=[];size=sum(row['bytes'] for row in files.values());now=self.clock()
        for name,host in self.hosts.items():
            if name==destination['host'] or name in exclude:continue
            for cache in self.entries(name):
                if cache['files']!=files:continue
                with self.guard:
                    if self.cooldown.get((name,destination['host']),0)>now:break
                    seconds_per_byte,_=self.transfers.get((name,destination['host']),(0,0))
                    active=sum(count for (source,_),(rate,count) in self.transfers.items() if source==name)
                tie=hashlib.sha256((destination['host']+name+cache['key']).encode()).hexdigest()
                candidates.append(((active,seconds_per_byte*size,tie),host,cache));break
        return [(host,cache) for _,host,cache in sorted(candidates,key=lambda row:row[0])]

    def transfer_started(self, source, destination):
        key=(source['host'],destination['host'])
        with self.guard:
            rate,active=self.transfers.get(key,(0,0));self.transfers[key]=(rate,active+1)

    def transfer_finished(self, source, destination, size, seconds, *, success):
        key=(source['host'],destination['host'])
        with self.guard:
            rate,active=self.transfers.get(key,(0,0))
            if success:
                measured=max(seconds,.000001)/max(size,1);rate=.7*rate+.3*measured if rate else measured
            else:self.cooldown[key]=self.clock()+60
            self.transfers[key]=(rate,max(0,active-1))

    @staticmethod
    def derived_files(identity, image, *, directory=None):
        """Reuse the existing derivation metadata, never create another ownership index."""
        encoded=json.dumps({'identity':identity,'image':image},sort_keys=True,separators=(',',':'))
        path=Path(directory or Path.home()/'.local/state/swarm-control/submission/shared-objects')/(hashlib.sha256(encoded.encode()).hexdigest()+'.json')
        try:
            if path.is_symlink() or path.stat().st_size>65536:return None
            record=json.loads(path.read_text());cache=record['cache']
            if record['identity']!=identity or cache['image']!=image:return None
            key,_=contract(image,cache['files'])
            return cache['files'] if cache['key']==key else None
        except (OSError,ValueError,KeyError,TypeError):return None
