"""Share complete Docker images through bounded launcher pipes; SSH is bootstrap-only."""
from collections import deque
import hashlib
import json
import os
import re
import shlex
import subprocess
import time
import threading
import fcntl
from pathlib import Path

class ImageResolver:
    def __init__(self, hosts=(), ssh=None, cancelled=lambda:False, notify=lambda *args:None, builders=None):
        self.hosts, self.ssh, self.cancelled, self.notify = hosts, ssh, cancelled, notify
        self.builders = builders or {}
        self.images = {}
        self.host_images = {}

    def command(self, host, command):
        if host and host.get('agent_endpoint') and host.get('machine'):
            from scripts.swarm_runtime import shell_command
            return shell_command(host,command)
        return self.ssh(host) + [shlex.join(command)] if host else command

    @staticmethod
    def cache_tag(image):
        return 'docker-utils-digest:'+hashlib.sha256(image.encode()).hexdigest() if '@sha256:' in image or image.startswith('sha256:') else image

    def inspect(self, host, image):
        for reference in dict.fromkeys((image,self.cache_tag(image))):
            try:
                result = subprocess.run(self.command(host, ['docker', 'image', 'inspect', reference, '--format', '{{json .Id}}']),
                                        capture_output=True, text=True, timeout=25)
                if result.returncode:continue
                image_id = json.loads(result.stdout)
                if isinstance(image_id,str) and re.fullmatch(r'sha256:[a-f0-9]{64}',image_id):return image_id
            except (OSError,subprocess.TimeoutExpired,ValueError):pass
        return None

    def fingerprint(self, host, image):
        try:
            reference = self.inspect(host,image) or image
            result = subprocess.run(self.command(host, ['docker', 'image', 'inspect', reference]),
                                    capture_output=True, text=True, timeout=25)
            if result.returncode:return None
            metadata = json.loads(result.stdout)[0]
            content = {key: metadata[key] for key in ('RootFS', 'Config', 'Architecture', 'Os', 'Created')}
            content['Config'] = {key: value for key, value in content['Config'].items() if value is not None}
            # Docker API versions omit defaults that newer clients materialize.
            # Normalize only equivalent defaults; preserve every nondefault
            # setting and the complete filesystem layer identity.
            for name in ('AttachStdin','AttachStdout','AttachStderr','StdinOnce','Tty','OpenStdin'):
                content['Config'][name]=content['Config'].get(name) or False
            for name in ('WorkingDir','User','Image','Domainname','Hostname'):
                content['Config'][name]=content['Config'].get(name) or ''
            return hashlib.sha256(json.dumps(content, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        except (OSError,subprocess.TimeoutExpired,ValueError,KeyError,IndexError):
            return None

    def compatible(self, host, image):
        try:
            local=subprocess.run(['docker','info','--format','{{.OSType}}/{{.Architecture}}'],capture_output=True,text=True,timeout=25)
            reference=self.inspect(host,image) or image
            remote=subprocess.run(self.command(host,['docker','image','inspect',reference,'--format','{{.Os}}/{{.Architecture}}']),capture_output=True,text=True,timeout=25)
            normalize=lambda value:value.strip().replace('x86_64','amd64').replace('aarch64','arm64')
            return local.returncode==0 and remote.returncode==0 and normalize(local.stdout)==normalize(remote.stdout)
        except (OSError,subprocess.TimeoutExpired):return False

    def wait(self, processes, timeout):
        deadline = time.monotonic() + timeout
        output = {process: deque(maxlen=20) for process in processes}
        readers = []

        def drain(process, stream):
            for line in iter(stream.readline, b''):
                output[process].append(line.decode(errors='replace')[:2000])
            stream.close()

        for process in processes:
            for stream in (process.stdout, process.stderr):
                if stream:
                    reader = threading.Thread(target=drain, args=(process, stream), daemon=True)
                    reader.start()
                    readers.append(reader)
        try:
            while any(process.poll() is None for process in processes):
                if self.cancelled():
                    raise InterruptedError('Image preparation stopped')
                if time.monotonic() >= deadline:
                    raise TimeoutError('Image preparation timed out')
                time.sleep(0.2)
            for reader in readers:
                reader.join(timeout=5)
            for process in processes:
                if process.returncode:
                    raise RuntimeError(''.join(output[process])[-2000:] or 'Image command failed')
        finally:
            for process in processes:
                if process.poll() is None:
                    process.terminate()
            for process in processes:
                if process.poll() is None:
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()

    def local_command(self, command, input_text=None):
        # Pull/build output is drained continuously; Docker progress cannot fill a pipe.
        process = subprocess.Popen(command, stdin=subprocess.PIPE if input_text else subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                   env={**os.environ,'DOCKER_BUILDKIT':'0'} if command[:2]==['docker','build'] else None)
        if input_text:
            process.stdin.write(input_text.encode())
            process.stdin.close()
            process.stdin = None
        self.wait([process], 1800)

    def transfer(self, source, destination, image, expected):
        if self.cancelled():
            raise InterruptedError('Image distribution stopped')
        source_name = source['host'] if source else 'compai.local'
        target_name = destination['host'] if destination else 'compai.local'
        self.notify(image, 'distributing', source_name, target_name)
        # The tar stream travels through OS pipes only. No archive is written to disk.
        reference = self.cache_tag(image)
        if reference != image:
            subprocess.run(self.command(source,['docker','tag',expected,reference]),check=True,capture_output=True,timeout=25)
        export = 'docker image save ' + shlex.quote(reference) + ' | gzip -1'
        load = 'gzip -d | docker image load --quiet'
        producer = subprocess.Popen(self.command(source, ['bash', '-o', 'pipefail', '-c', export]),
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        consumer = None
        try:
            consumer = subprocess.Popen(self.command(destination, ['bash', '-o', 'pipefail', '-c', load]),
                                        stdin=producer.stdout, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            producer.stdout.close()
            producer.stdout = None
            self.wait([producer, consumer], 1800)
            if self.inspect(destination, image) != expected and (not self.fingerprint(source, image) or self.fingerprint(source, image) != self.fingerprint(destination, image)):
                raise RuntimeError(f'Image ID verification failed on {target_name}: {image}')
        finally:
            for process in (producer, consumer):
                if process and process.poll() is None:
                    process.kill()
                    process.wait()

    def ensure(self, image, destinations):
        if self.cancelled():
            raise InterruptedError('Image preparation stopped')
        self.notify(image, 'locating', None, None)
        source, expected = None, self.inspect(None, image)
        if expected and not self.compatible(None,image):expected=None
        if not expected:
            for host in self.hosts:
                expected = self.inspect(host, image)
                if expected and not self.compatible(host,image):expected=None
                if expected:
                    source = host
                    break
        if not expected:
            if image in self.builders:
                self.notify(image, 'building', 'local', None)
                self.builders[image](self)
            else:
                self.notify(image, 'pulling', 'local', None)
                self.local_command(['docker', 'image', 'pull', image])
            expected = self.inspect(None, image)
            if not expected or not self.compatible(None,image):
                raise RuntimeError('Image preparation did not produce a complete native-platform image: ' + image)
        self.notify(image, 'cached', source['host'] if source else 'compai.local', None)
        for host in destinations:
            actual = self.inspect(host, image)
            if actual != expected and (not actual or not self.fingerprint(source, image) or self.fingerprint(source, image) != self.fingerprint(host, image)):
                folder=Path.home()/'.local/state/swarm-control/image-distribution'
                folder.mkdir(parents=True,exist_ok=True,mode=0o700)
                identity={'destination':host.get('machine',host['host']) if host else 'local','image':expected}
                key=hashlib.sha256(json.dumps(identity,sort_keys=True).encode()).hexdigest()
                with (folder/(key+'.lock')).open('a+') as lock:
                    fcntl.flock(lock,fcntl.LOCK_EX)
                    actual=self.inspect(host,image)
                    if actual!=expected and (not actual or not self.fingerprint(source,image) or self.fingerprint(source,image)!=self.fingerprint(host,image)):
                        self.transfer(source, host, image, expected)
            self.host_images.setdefault(host['host'] if host else 'local', {})[image] = self.inspect(host, image)
        self.images[image] = expected
        self.notify(image, 'ready', source['host'] if source else 'compai.local', None)
        return expected

