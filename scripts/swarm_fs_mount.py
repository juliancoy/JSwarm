"""Mount a private node filesystem with local and peer RAM read caches.

Host requirements: system libfuse2 or libfuse3, fusermount, and mfusepy. No signing key is needed.
"""
import base64
import errno
import hashlib
import json
import os
from pathlib import Path
import sys
import time

PROJECT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(PROJECT), str(PROJECT/'swarm-control/server')]
from scripts.swarm_filesystem import BLOCK, block_key, request
from scripts.run_input_cache import ram_cache


class Client:
    def __init__(self, settings, factory=None, cache=None):
        self.settings = settings
        self.clients = {}
        self.factory = factory
        self.cache = cache or ram_cache(16*1024**2)
        self.cooldown = {}
        self.counters = {'local_hits': 0, 'peer_hits': 0, 'origin_reads': 0, 'durable_writes': 0, 'peer_errors': 0, 'peer_misses': 0, 'peer_admission_failures': 0}

    def call(self, node, operation, *, timeout=None, **args):
        if self.settings.get('local_gateway'):
            import socket
            from scripts.swarm_filesystem import canonical
            value = request(self.settings, 'route', node=node, action=operation, args=args)
            path = Path.home()/'.config/lifetech-launcher/filesystem.sock'
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
                connection.settimeout(timeout or (3 if operation.startswith('cache.') else 65));connection.connect(str(path));connection.sendall(canonical(value)+b'\n')
                with connection.makefile('rb') as stream:raw = stream.readline(1024**2)
            reply = json.loads(raw)
            if not reply['ok']:raise OSError(errno.EIO, reply.get('error', 'Filesystem gateway failed'))
            wrapped = reply['result']
            if 'errno' in wrapped:raise OSError(wrapped['errno'], os.strerror(wrapped['errno']))
            value = wrapped['result']
            if 'errno' in value:raise OSError(value['errno'], os.strerror(value['errno']))
            return value['result']
        if node not in self.clients:
            from scripts.launcher_discovery import resolve_endpoint
            from agent_client import AgentClient
            endpoint, _ = resolve_endpoint(node)
            self.clients[node] = (self.factory or AgentClient)({'host': node, 'agent_endpoint': endpoint})
        value = self.clients[node].call('filesystem.rpc', request(self.settings, operation, **args), timeout=timeout or (self.settings.get('cache_timeout', 3) if operation.startswith('cache.') else 60))
        if 'errno' in value:raise OSError(value['errno'], os.strerror(value['errno']))
        return value['result']

    def origin(self, operation, **args):return self.call(self.settings['origin'], operation, **args)

    def read(self, path, size, offset):
        st = self.origin('stat', path=path)
        stop = min(st['st_size'], offset+size)
        result = bytearray()
        while offset < stop:
            start = offset//BLOCK*BLOCK
            length = min(BLOCK, st['st_size']-start)
            key = block_key(self.settings['namespace'], path, st['version'], start)
            def fetch(*_):
                deadline = time.monotonic()+self.settings.get('cache_probe_budget', 3)
                for peer in self.settings.get('peers', []):
                    if time.monotonic() >= deadline:break
                    if self.cooldown.get(peer, 0) > time.monotonic():continue
                    try:
                        row = self.call(peer, 'cache.get', timeout=max(.05,deadline-time.monotonic()), key=key, length=length)
                        if row['data'] is not None:
                            data = base64.b64decode(row['data'], validate=True)
                            if len(data) != length:raise ValueError('Peer block length differs')
                            self.counters['peer_hits'] += 1
                            return data
                        self.counters['peer_misses'] += 1
                    except (OSError, RuntimeError, ValueError):
                        self.counters['peer_errors'] += 1
                        self.cooldown[peer] = time.monotonic()+2
                row = self.origin('read', path=path, version=st['version'], offset=start, length=length)
                data = base64.b64decode(row['data'], validate=True)
                if len(data) != length or hashlib.sha256(data).hexdigest() != row['sha256']:raise OSError(errno.EIO, 'Origin block verification failed')
                self.counters['origin_reads'] += 1
                # One remote copy is enough; avoid multiplying every read across
                # the fleet. Peer failures never prevent an origin-backed read.
                for peer in self.settings.get('peers', []):
                    if time.monotonic() >= deadline:break
                    if self.cooldown.get(peer, 0) > time.monotonic():continue
                    try:
                        admission = self.call(peer, 'cache.put', timeout=max(.05,deadline-time.monotonic()), key=key, length=length, data=row['data'])
                        if admission.get('stored', False):break
                        self.counters['peer_admission_failures'] += 1
                    except (OSError, RuntimeError, ValueError):self.cooldown[peer] = time.monotonic()+2
                return data
            data = self.cache.lookup(key, length, 0)
            if data is None:data = self.cache.block(key, length, 0, fetch)
            else:self.counters['local_hits'] += 1
            count = min(stop-offset, len(data)-(offset-start))
            result.extend(data[offset-start:offset-start+count]);offset += count
        return bytes(result)

    def write(self, path, data, offset):
        st = self.origin('stat', path=path)
        written = 0
        while written < len(data):
            chunk = data[written:written+BLOCK]
            try:
                st = self.origin('write', path=path, version=st['version'], offset=offset+written,
                                 data=base64.b64encode(chunk).decode())
            except OSError:
                if written:return written
                raise
            count = st['written']
            if count <= 0:raise OSError(errno.EIO, 'Write made no progress')
            start = offset+written
            length = min(BLOCK, st['st_size']-start)
            if start % BLOCK == 0 and count == length:
                key = block_key(self.settings['namespace'], path, st['version'], start)
                self.cache.block(key, length, 0, lambda *_: chunk[:count])
            written += count;self.counters['durable_writes'] += 1
        return written


def immutable_ram_snapshot(client, path, sha256, *, maximum_bytes=512*1024**2):
    """Context manager for mmap tools: verified immutable tmpfs, never disk.

    Deliberately separate from the mutable FUSE view, which uses direct IO to
    observe writes from other nodes. Callers supply the published content hash.
    """
    import contextlib
    import shutil
    import tempfile
    @contextlib.contextmanager
    def snapshot():
        if len(sha256) != 64 or any(c not in '0123456789abcdef' for c in sha256):raise ValueError('Expected SHA-256 required')
        before = client.origin('stat', path=path)
        size = before['st_size']
        if size > maximum_bytes or shutil.disk_usage('/dev/shm').free < size+128*1024**2:
            raise OSError(errno.ENOSPC, 'Immutable snapshot exceeds RAM admission')
        with tempfile.TemporaryDirectory(prefix='swarm-snapshot-', dir='/dev/shm') as folder:
            target = Path(folder)/'data'
            digest = hashlib.sha256()
            with target.open('xb') as stream:
                for offset in range(0, size, BLOCK):
                    chunk = client.read(path, min(BLOCK, size-offset), offset)
                    if len(chunk) != min(BLOCK, size-offset):raise OSError(errno.ESTALE, 'Snapshot input changed')
                    digest.update(chunk);stream.write(chunk)
            if digest.hexdigest() != sha256 or client.origin('stat', path=path)['version'] != before['version']:
                raise OSError(errno.ESTALE, 'Snapshot content or version changed')
            target.chmod(0o400)
            yield target
    return snapshot()


def select_system_fuse(settings):
    import ctypes.util
    requested = str(settings.get('fuse_version', 'auto'))
    if requested not in ('auto', '2', '3'):raise ValueError('fuse_version must be auto, 2 or 3')
    names = ['fuse3', 'fuse'] if requested == 'auto' else ['fuse3' if requested == '3' else 'fuse']
    for name in names:
        library = ctypes.util.find_library(name)
        if library:
            os.environ['FUSE_LIBRARY_PATH'] = library
            return library
    raise OSError(errno.ENODEV, 'No compatible system FUSE library installed')


def mount(settings, mountpoint, foreground=False):
    select_system_fuse(settings)
    from mfusepy import FUSE, FuseOSError, Operations
    if settings.get('local_gateway'):
        gateway = Path.home()/'.config/lifetech-launcher/filesystem.sock'
        deadline = time.monotonic()+90
        while not gateway.exists():
            if time.monotonic() > deadline:raise OSError(errno.ETIMEDOUT, 'Node filesystem gateway unavailable')
            time.sleep(.25)
    client = Client(settings)
    def checked(function):
        def wrapped(*args, **kwargs):
            try:return function(*args, **kwargs)
            except OSError as error:raise FuseOSError(error.errno or errno.EIO)
            except (RuntimeError, ValueError):raise FuseOSError(errno.EIO)
        return wrapped
    class Files(Operations):
        @checked
        def statfs(self, path):return client.origin('statfs')
        @checked
        def getattr(self, path, fh=None):
            row = client.origin('stat', path=path)
            row['st_uid'] = os.getuid();row['st_gid'] = os.getgid()
            return row
        @checked
        def readdir(self, path, fh):return ['.', '..', *client.origin('list', path=path)['names']]
        @checked
        def open(self, path, flags):
            if flags & os.O_APPEND:raise OSError(errno.EOPNOTSUPP, 'Atomic append is not supported')
            client.origin('stat', path=path);return 0
        @checked
        def create(self, path, mode, fi=None):client.origin('create', path=path);return 0
        @checked
        def read(self, path, size, offset, fh):return client.read(path, size, offset)
        @checked
        def write(self, path, data, offset, fh):return client.write(path, data, offset)
        @checked
        def truncate(self, path, length, fh=None):
            st = client.origin('stat', path=path)
            client.origin('truncate', path=path, size=length, version=st['version'])
        @checked
        def mkdir(self, path, mode):client.origin('mkdir', path=path)
        @checked
        def unlink(self, path):client.origin('unlink', path=path)
        @checked
        def rmdir(self, path):client.origin('rmdir', path=path)
        @checked
        def rename(self, old, new):client.origin('rename', path=old, destination=new)
        @checked
        def chmod(self, path, mode):client.origin('chmod', path=path, mode=mode & 0o7777)
        def flush(self, path, fh):return 0
        def fsync(self, path, datasync, fh):return 0  # Every write already fsyncs at origin.
    FUSE(Files(), str(mountpoint), foreground=foreground, nothreads=True,
         attr_timeout=0, entry_timeout=0, negative_timeout=0, direct_io=True,
         fsname='swarm-'+settings['namespace'], default_permissions=True,
         allow_root=settings.get('allow_root', True))


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mountpoint', type=Path)
    parser.add_argument('--config', type=Path, default=Path.home()/'.config/lifetech-launcher/filesystem.json')
    parser.add_argument('--foreground', action='store_true')
    args = parser.parse_args()
    mount(json.loads(args.config.read_text()), args.mountpoint, args.foreground)
