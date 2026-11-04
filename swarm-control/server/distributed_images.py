"""Workspace suite definitions on top of the shared LAN-first Docker module."""
from paths import WORKSPACE
from pathlib import Path
import sys
import shlex
import threading
from image_registry import ImageRegistry
from concurrent.futures import ThreadPoolExecutor
sys.path.insert(0,str(WORKSPACE))
from docker_utils.images import ImageResolver
from docker_utils.policy import prepare_build
from distributed_worker import NODE,PYTHON,SUITES

TOOLCHAIN='orgportal-distributed-python-node:1'


def required_images(host):return list(dict.fromkeys(SUITES[suite]['image'] for suite in host['suites']))


class ImageDistributor(ImageResolver):
    def __init__(self,hosts,ssh,cancelled,notify):
        self.refreshed=set()
        self.image_locks={}
        self.locks_guard=threading.Lock()
        self.registry=ImageRegistry(self)
        self.failures={}
        super().__init__(hosts,ssh,cancelled,notify,{
            TOOLCHAIN:self.toolchain,
            'organization-launcher:webrtc-1':self.launcher,
        })

    def ensure(self,image,destinations,*,isolate_hosts=False):
        with self.locks_guard:lock=self.image_locks.setdefault(image,threading.RLock())
        with lock:self.refresh(image)
        return self.ensure_locked(image,destinations,isolate_hosts=isolate_hosts)

    def refresh(self,image):
        # A run refreshes each required registry reference once, then shares its
        # verified contents over the LAN. Pull failures never silently use stale tags.
        if image not in self.refreshed:
            if self.cancelled():raise InterruptedError('Image preparation stopped')
            if image in self.builders:
                self.notify(image,'building','compai.local',None)
                self.builders[image](self)
            else:
                self.notify(image,'pulling','compai.local',None)
                self.local_command(['docker','image','pull',image])
                self.notify(image,'downloaded','compai.local',None)
            if not self.inspect(None,image) or not self.compatible(None,image):
                raise RuntimeError('Image refresh did not produce a native-platform image: '+image)
            self.refreshed.add(image)

    def ensure_locked(self,image,destinations,*,isolate_hosts=False):
        def distribute(host):
            try:return super(ImageDistributor,self).ensure(image,[host])
            except InterruptedError:raise
            except Exception as error:
                if not isolate_hosts or host is None:raise
                self.failures.setdefault(host['host'],{'stage':'image_preparation','reason':type(error).__name__+': '+str(error)[:600],'image':image})
                self.notify(image,'failed',None,host['host'])
                return None
        if len(destinations)<2:
            for host in destinations:distribute(host)
        else:
            with ThreadPoolExecutor(max_workers=2) as pool:list(pool.map(distribute,destinations))
        return self.images.get(image) or self.inspect(None,image)

    def inspect(self,host,reference):
        if host and host.get('agent_endpoint'):
            value=self.remote_image(host,reference)
            return value['id'] if value else None
        return super().inspect(host,reference)

    def remote_image(self,host,reference):
        from agent_client import AgentClient
        import time
        for attempt in range(3):
            if self.cancelled():raise InterruptedError('Image verification stopped')
            client=AgentClient(host)
            # The launcher's Docker SDK permits 120s; the authenticated caller
            # must allow that response rather than abandon it after 30s.
            try:return client.call('image.inspect',{'image':reference},timeout=150)
            except (OSError,RuntimeError) as error:
                transient=isinstance(error,(TimeoutError,ConnectionError)) or str(error).startswith('TimeoutExpired:')
                if not transient or attempt==2:raise
                self.notify(reference,'waiting_for_agent',None,host['host'])
                time.sleep(1+attempt)
            finally:client.close()

    def compatible(self,host,reference):
        if host and host.get('agent_endpoint'):
            import json,subprocess
            value=self.remote_image(host,reference)
            if not value:return False
            local=json.loads(subprocess.check_output(['docker','image','inspect',reference],text=True,timeout=25))[0]
            return (local['Os'],local['Architecture'])==(value['os'],value['architecture'])
        return super().compatible(host,reference)

    def fingerprint(self,host,reference):
        if host and host.get('agent_endpoint'):
            value=self.remote_image(host,reference)
            return value['fingerprint'] if value else None
        return super().fingerprint(host,reference)

    def transfer(self,source,destination,image,expected):
        if destination is None:return super().transfer(source,destination,image,expected)
        return self.registry.transfer(source,destination,image,expected)

    def toolchain(self,service):
        for base in (NODE,PYTHON):self.ensure(base,[None])
        node=self.host_images['local'][NODE]
        python=self.host_images['local'][PYTHON]
        dockerfile=f'FROM {node} AS node\nFROM {python}\nCOPY --from=node /usr/local/bin/node /usr/local/bin/node\n'
        self.local_command(['docker','build','--pull=false','--network=none','-t',TOOLCHAIN,'-'],dockerfile)

    def launcher(self,service):
        dockerfile=WORKSPACE/'Dockerfile.launcher'
        text=dockerfile.read_text()
        for line in text.splitlines():
            tokens=shlex.split(line,comments=True)
            if tokens and tokens[0].upper()=='FROM':self.ensure(tokens[1],[None])
        text=prepare_build(text)
        self.local_command(['docker','build','--pull=false','-t','organization-launcher:webrtc-1','-'],text)

    def prepare(self):
        images=list(dict.fromkeys(image for host in self.hosts for image in required_images(host)))
        for image in images:
            destinations=[host for host in self.hosts if host['host'] not in self.failures and image in required_images(host)]
            if not destinations:continue
            try:self.ensure(image,destinations,isolate_hosts=True)
            except InterruptedError:raise
            except Exception as error:
                for host in destinations:
                    self.failures.setdefault(host['host'],{'stage':'image_preparation','reason':type(error).__name__+': '+str(error)[:600],'image':image})
                    self.notify(image,'failed',None,host['host'])
        return dict(self.images)
