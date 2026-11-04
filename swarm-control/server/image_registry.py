"""Publish once to a loopback registry; agents pull missing layers over encrypted RPC."""
import concurrent.futures
import hashlib
import json
import re
import subprocess
import threading
import time
import urllib.request

IMAGE='registry:3'
NAME='organization-test-registry'
PORT=18791


class ImageRegistry:
    def __init__(self,service):
        self.service=service
        self.lock=threading.Lock()
        self.ready=False
        self.published={}
        self.seeders={}
        self.bootstrap_locks={}

    def start(self):
        if self.ready:return
        self.service.ensure(IMAGE,[None])
        image=self.service.inspect(None,IMAGE)
        result=subprocess.run(['docker','inspect',NAME],capture_output=True,text=True,timeout=25)
        if result.returncode==0:
            current=json.loads(result.stdout)[0]
            if (current['Config'].get('Labels') or {}).get('organization.role')!='test-registry':
                raise RuntimeError('Registry container name is already owned by another service')
            bindings=current['HostConfig'].get('PortBindings',{}).get('5000/tcp',[])
            if bindings!=[{'HostIp':'127.0.0.1','HostPort':str(PORT)}]:
                raise RuntimeError('Test registry must listen only on the expected loopback port')
            if current['Image']!=image:
                self.service.local_command(['docker','rm','-f',NAME])
                result=None
            elif not current['State']['Running']:
                self.service.local_command(['docker','start',NAME])
        if result is None or result.returncode!=0:
            self.service.local_command(['docker','run','--pull=never','-d','--name',NAME,
                '--restart','unless-stopped','--label','organization.role=test-registry',
                '--publish',f'127.0.0.1:{PORT}:5000','--memory','256m','--pids-limit','128',
                '--log-opt','max-size=2m','--log-opt','max-file=2',
                '--mount','type=volume,src=organization-test-registry,dst=/var/lib/registry',image])
        for _ in range(60):
            if self.service.cancelled():raise InterruptedError('Registry preparation stopped')
            try:
                with urllib.request.urlopen(f'http://127.0.0.1:{PORT}/v2/',timeout=2) as response:
                    if response.status==200:self.ready=True;return
            except OSError:pass
            time.sleep(.5)
        raise RuntimeError('Loopback image registry did not become ready')

    def publish(self,image,expected):
        with self.lock:
            key=(image,expected)
            if key in self.published:return self.published[key]
            self.start()
            fingerprint=self.service.fingerprint(None,expected)
            if not fingerprint:raise RuntimeError('Image contents unavailable for registry publication')
            repository='tests/'+hashlib.sha256(image.encode()).hexdigest()[:24]
            tag=f'127.0.0.1:{PORT}/{repository}:{fingerprint}'
            self.service.notify(image,'publishing','compai.local',None)
            self.service.local_command(['docker','tag',expected,tag])
            self.service.local_command(['docker','push',tag])
            request=urllib.request.Request(f'http://127.0.0.1:{PORT}/v2/{repository}/manifests/{fingerprint}',
                method='HEAD',headers={'Accept':'application/vnd.oci.image.index.v1+json, application/vnd.oci.image.manifest.v1+json, application/vnd.docker.distribution.manifest.list.v2+json, application/vnd.docker.distribution.manifest.v2+json'})
            opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(request,timeout=15) as response:digest=response.headers.get('Docker-Content-Digest')
            if not digest or not re.fullmatch(r'sha256:[a-f0-9]{64}',digest):
                raise RuntimeError('Registry did not return an immutable image digest')
            # Verify the exact reference agents will use, not just the mutable tag.
            request=urllib.request.Request(f'http://127.0.0.1:{PORT}/v2/{repository}/manifests/{digest}',
                headers={'Accept':'application/vnd.oci.image.index.v1+json, application/vnd.oci.image.manifest.v1+json, application/vnd.docker.distribution.manifest.list.v2+json, application/vnd.docker.distribution.manifest.v2+json'})
            with opener.open(request,timeout=15) as response:
                manifest=response.read(4*1024*1024+1)
            if len(manifest)>4*1024*1024 or 'sha256:'+hashlib.sha256(manifest).hexdigest()!=digest:
                raise RuntimeError('Published registry manifest does not match its immutable digest: '+image)
            value=(repository,digest,fingerprint)
            self.published[key]=value
            return value

    def transfer(self,source,destination,image,expected):
        if source is not None or not destination or not destination.get('agent_endpoint'):
            raise RuntimeError('Layer delivery requires a coordinator image and enrolled agent destination')
        from agent_client import AgentClient,CONFIG
        repository,digest,fingerprint=self.publish(image,expected)
        key=(image,fingerprint)
        with self.lock:gate=self.bootstrap_locks.setdefault(key,threading.Lock())
        # Seed one recipient first; concurrent cold workers then fetch from it
        # instead of all starting duplicate coordinator transfers.
        while not gate.acquire(timeout=1):
            if self.service.cancelled():raise InterruptedError('Image preparation stopped')
        try:
            if not self.seeders.get(key):return self.deliver(destination,image,repository,digest,fingerprint)
        finally:gate.release()
        return self.deliver(destination,image,repository,digest,fingerprint)

    def deliver(self,destination,image,repository,digest,fingerprint):
        from agent_client import AgentClient,CONFIG
        coordinator=json.loads(CONFIG.read_text())['name']
        self.service.notify(image,'distributing_layers',coordinator,destination['host'])
        client=AgentClient(destination);progress_client=AgentClient(destination)
        sources=list(dict.fromkeys(self.seeders.get((image,fingerprint),[])+[name for name,images in list(self.service.host_images.items()) if name!='local' and image in images]))
        sources=[name for name in sources if name!=destination['host'] and name!=coordinator]
        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                future=pool.submit(client.call,'image.pull',{'coordinator':coordinator,'repository':repository,
                    'digest':digest,'fingerprint':fingerprint,'image':image,'sources':sources},timeout=1800)
                while True:
                    try:
                        result=future.result(timeout=2)
                        break
                    except concurrent.futures.TimeoutError:
                        if future.done():raise
                        try:progress=progress_client.call('image.progress',{'image':image},timeout=10)
                        except Exception:progress={}
                        self.service.notify(image,'distributing_layers',coordinator,destination['host'],progress)
        finally:client.close();progress_client.close()
        if not result.get('id'):raise RuntimeError('Agent did not confirm its layer-verified image')
        self.seeders.setdefault((image,fingerprint),[])
        if destination['host'] not in self.seeders[(image,fingerprint)]:self.seeders[(image,fingerprint)].append(destination['host'])
        sources=result.get('source_bytes',{})
        actual_source=max(sources,key=sources.get) if sources else coordinator
        self.service.notify(image,'transferred',actual_source,destination['host'],{'source_bytes':sources})
