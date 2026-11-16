"""Mount a private node filesystem with local and peer RAM read caches.

Host requirements: system libfuse2 or libfuse3, fusermount, and mfusepy. No signing key is needed.
"""
import base64
import errno
import hashlib
import json
import os
import queue
import threading
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
        self.immutable_stats = {}
        self.peer_miss_until = {}
        self.publish_queue = None
        self.publish_guard = threading.Lock()
        self.immutable_reader = None
        if settings.get('immutable_bulk_files'):
            from scripts.swarm_immutable_read import Reader
            if set(settings['immutable_bulk_files'])-set(settings.get('immutable_files',{})):
                raise ValueError('Bulk ranges require pinned immutable file versions')
            self.immutable_reader=Reader(settings['immutable_bulk_files'])
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

    def stat(self, path):
        row=self.origin('stat',path=path)
        expected=self.settings.get('immutable_files',{}).get(path)
        if expected is not None and (row['version']!=expected or row['st_mode'] & 0o222):
            raise OSError(errno.ESTALE,'Pinned immutable file changed')
        if expected is not None:self.immutable_stats[path]=row
        return row

    def open_flags(self, path, flags):
        self.stat(path)
        immutable=path in self.settings.get('immutable_files',{})
        if immutable and flags & os.O_ACCMODE != os.O_RDONLY:
            raise OSError(errno.EROFS,'Pinned reference is read-only')
        return {'direct_io':not immutable,'keep_cache':immutable}

    def publish_immutable(self,key,data):
        # A peer replica must never hold a page fault open. Bound queued RAM to
        # 64 small pages and reuse one publisher for the node filesystem client.
        with self.publish_guard:
            if self.publish_queue is None:
                self.publish_queue=queue.Queue(maxsize=64)
                threading.Thread(target=self._publish_immutable,daemon=True,name='filesystem-peer-publish').start()
        try:self.publish_queue.put_nowait((key,data))
        except queue.Full:pass

    def _publish_immutable(self):
        while True:
            key,data=self.publish_queue.get()
            try:
                for peer in self.settings.get('peers',[]):
                    if self.cooldown.get(peer,0)>time.monotonic():continue
                    try:
                        row=self.call(peer,'cache.put',timeout=1,key=key,length=len(data),data=base64.b64encode(data).decode())
                        if row.get('stored'):break
                        self.counters['peer_admission_failures']+=1
                    except (OSError,RuntimeError,ValueError):
                        self.cooldown[peer]=time.monotonic()+2
            finally:self.publish_queue.task_done()

    def read_immutable(self,path,size,offset,st):
        from scripts.swarm_immutable_read import IntegrityError
        page=16*1024;stop=min(st['st_size'],offset+size);result=bytearray()
        revision=st['version']+':page16k'
        while offset<stop:
            start=offset//page*page;length=min(page,st['st_size']-start)
            key=block_key(self.settings['namespace'],path,revision,start)
            data=self.cache.lookup(key,length,0)
            if data is not None:
                self.counters['local_hits']+=1
            else:
                for peer in self.settings.get('peers',[]):
                    if max(self.cooldown.get(peer,0),self.peer_miss_until.get(peer,0))>time.monotonic():continue
                    try:
                        row=self.call(peer,'cache.get',timeout=.5,key=key,length=length)
                        if row['data'] is not None:
                            data=base64.b64decode(row['data'],validate=True)
                            if len(data)!=length:raise ValueError('Peer immutable page length differs')
                            self.counters['peer_hits']+=1;break
                        self.counters['peer_misses']+=1
                        self.peer_miss_until[peer]=time.monotonic()+30
                    except (OSError,RuntimeError,ValueError):self.cooldown[peer]=time.monotonic()+2
                if data is None:
                    end=min(st['st_size'],((stop+page-1)//page)*page,start+BLOCK)
                    try:blob=self.immutable_reader.read(path,start,end-start)
                    except IntegrityError:raise
                    except (OSError,RuntimeError):
                        row=self.origin('read',path=path,version=st['version'],offset=start,length=end-start)
                        blob=base64.b64decode(row['data'],validate=True)
                        if len(blob)!=end-start or hashlib.sha256(blob).hexdigest()!=row['sha256']:raise OSError(errno.EIO,'Immutable origin verification failed')
                    self.counters['origin_reads']+=1
                    # One coalesced range fills the same shared 16KiB page keys.
                    for position in range(start,end,page):
                        part=blob[position-start:position-start+page]
                        part_key=block_key(self.settings['namespace'],path,revision,position)
                        self.cache.block(part_key,len(part),0,lambda *_,part=part:part)
                        self.publish_immutable(part_key,part)
                    data=blob[:length]
                else:self.cache.block(key,length,0,lambda *_:data)
            count=min(stop-offset,len(data)-(offset-start))
            result.extend(data[offset-start:offset-start+count]);offset+=count
        return bytes(result)

    def read(self, path, size, offset):
        immutable=path in self.settings.get('immutable_files',{})
        st = self.immutable_stats.get(path) if immutable else None
        if st is None:st=self.stat(path)
        if immutable and self.immutable_reader is not None and path in self.settings.get('immutable_bulk_files',{}):
            return self.read_immutable(path,size,offset,st)
        block_size=16*1024 if immutable else BLOCK
        stop = min(st['st_size'], offset+size)
        result = bytearray()
        while offset < stop:
            start = offset//block_size*block_size
            length = min(block_size, st['st_size']-start)
            revision=st['version']+':page16k' if immutable else st['version']
            key = block_key(self.settings['namespace'], path, revision, start)
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
            row = client.stat(path)
            row['st_uid'] = os.getuid();row['st_gid'] = os.getgid()
            return row
        @checked
        def readdir(self, path, fh):return ['.', '..', *client.origin('list', path=path)['names']]
        @checked
        def open(self, path, fi):
            if fi.flags & os.O_APPEND:raise OSError(errno.EOPNOTSUPP, 'Atomic append is not supported')
            policy=client.open_flags(path,fi.flags)
            fi.direct_io=policy['direct_io'];fi.keep_cache=policy['keep_cache'];fi.fh=0
            return 0
        @checked
        def create(self, path, mode, fi=None):
            client.origin('create', path=path)
            if fi is not None:fi.direct_io=True;fi.keep_cache=False;fi.fh=0
            return 0
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
         attr_timeout=0, entry_timeout=0, negative_timeout=0, raw_fi=True,
         fsname='swarm-'+settings['namespace'], default_permissions=True,
         allow_root=settings.get('allow_root', True) and not settings.get('allow_other', False),
         allow_other=settings.get('allow_other', False))


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mountpoint', type=Path)
    parser.add_argument('--config', type=Path, default=Path.home()/'.config/lifetech-launcher/filesystem.json')
    parser.add_argument('--foreground', action='store_true')
    args = parser.parse_args()
    mount(json.loads(args.config.read_text()), args.mountpoint, args.foreground)
