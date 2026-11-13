"""Private write-through filesystem, served by the existing node owner capsule.

The existing InputCache supplies bounded tmpfs storage. Cache keys include the
origin's file version; dirty data is never entrusted to an evictable RAM peer.
"""
import base64
import contextlib
import errno
import fcntl
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import shutil
import stat
import threading
import time

BLOCK = 256 * 1024
CACHE_SOURCE = ''


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':')).encode()


def request(settings, operation, **args):
    value = {'namespace': settings['namespace'], 'operation': operation,
             'args': args, 'time': time.time(), 'nonce': secrets.token_hex(16)}
    value['mac'] = hmac.new(bytes.fromhex(settings['secret']), canonical(value), 'sha256').hexdigest()
    return value


def version(st):
    return hashlib.sha256(canonical([st.st_dev, st.st_ino, st.st_size,
                                     st.st_mtime_ns, st.st_ctime_ns])).hexdigest()


def attributes(st):
    return {**{k: getattr(st, k) for k in ('st_mode', 'st_size', 'st_nlink',
              'st_uid', 'st_gid', 'st_atime', 'st_mtime', 'st_ctime')}, 'version': version(st)}


def block_key(namespace, path, revision, offset):
    return hashlib.sha256(canonical([namespace, path, revision, offset])).hexdigest()[:32]


class Filesystem:
    def __init__(self, config, cache=None):
        self.config = Path(config)
        if cache is None:
            import types
            module = types.ModuleType('filesystem_input_cache')
            source = CACHE_SOURCE or (Path(__file__).parent/'run_input_cache.py').read_text()
            exec(compile(source, 'run_input_cache.py', 'exec'), module.__dict__)
            cache = module.ram_cache(budget=16*1024**2)
            # Owner capsules have a bounded /dev/shm. The general host cache's
            # 128 MiB reserve would prohibit every admission on Docker's 64 MiB
            # tmpfs; preserve 8 MiB here while keeping the same LRU implementation.
            cache.reserve = 8*1024**2
        self.cache = cache
        self.seen = {}
        self.remotes = {}
        self.lock = threading.RLock()

    def dispatch(self, value):
        # Settings are reread so grants can be revoked without restarting a node.
        settings = json.loads(self.config.read_text())
        unsigned = {k: v for k, v in value.items() if k != 'mac'}
        expected = hmac.new(bytes.fromhex(settings['secret']), canonical(unsigned), 'sha256').hexdigest()
        if (set(value) != {'namespace', 'operation', 'args', 'time', 'nonce', 'mac'}
                or value['namespace'] != settings['namespace']
                or not hmac.compare_digest(expected, value['mac'])
                or abs(time.time()-value['time']) > 30):
            raise PermissionError('Invalid filesystem grant')
        with self.lock:
            now = time.time()
            self.seen = {k: t for k, t in self.seen.items() if now-t < 60}
            if value['nonce'] in self.seen:raise PermissionError('Replayed filesystem request')
            if len(self.seen) >= 65536:raise OSError(errno.EBUSY, 'Filesystem request limit')
            self.seen[value['nonce']] = now
        try:
            return {'result': self.operation(settings, value['operation'], value['args'])}
        except OSError as error:
            return {'errno': error.errno or errno.EIO}

    @contextlib.contextmanager
    def parent(self, root, path):
        # Walk with dirfds and O_NOFOLLOW, including the export root. Neither a
        # symlink nor a concurrent rename can redirect access outside the export.
        if not isinstance(path, str):raise OSError(errno.EINVAL, 'Invalid filesystem path')
        parts = path.split('/')
        if not isinstance(path, str) or len(path) > 4096 or any(p in ('.', '..') for p in parts) or '\0' in path:
            raise OSError(errno.EINVAL, 'Invalid filesystem path')
        parts = [p for p in parts if p]
        fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
        try:
            for component in [*Path(root).parts[1:], *parts[:-1]]:
                new = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                os.close(fd);fd = new
            yield fd, parts[-1] if parts else '.'
        finally:os.close(fd)

    def operation(self, settings, op, args):
        if op == 'route':
            # The FUSE adapter uses only host stdlib + fusepy. Network crypto
            # stays in the standard Python 3.12 node capsule, independent of the
            # host's Python version and without copying a controller key.
            import sys
            release = Path(settings['client_release'])
            sys.path[:0] = [str(release), str(release/'swarm-control/server')]
            from agent_client import AgentClient
            from scripts.launcher_discovery import resolve_endpoint
            node = args['node']
            if node not in [settings['origin'], *settings.get('peers', [])]:raise PermissionError('Unenrolled filesystem route')
            if args['action'] == 'route':raise ValueError('Recursive filesystem route')
            with self.lock:
                if node not in self.remotes:
                    endpoint, _ = resolve_endpoint(node)
                    self.remotes[node] = AgentClient({'host': node, 'agent_endpoint': endpoint})
                remote = self.remotes[node]
            return remote.call('filesystem.rpc', request(settings, args['action'], **args['args']),
                               timeout=3 if args['action'].startswith('cache.') else 60)
        if op in ('cache.get', 'cache.put'):
            key, length = args['key'], args['length']
            if type(length) is not int or not 0 < length <= BLOCK:raise ValueError('Invalid block size')
            if op == 'cache.get':data = self.cache.lookup(key, length, 0)
            else:
                data = base64.b64decode(args['data'], validate=True)
                if len(data) != length:raise ValueError('Invalid cache payload')
                self.cache.block(key, length, 0, lambda *_: data)
            return {'data': base64.b64encode(data).decode() if data is not None else None}
        if op == 'status':return {'namespace': settings['namespace'], 'origin': settings['origin'], 'write_policy': 'write-through'}
        root = settings.get('root')
        if not root:raise OSError(errno.EXDEV, 'This node is a RAM peer, not the origin')
        if op == 'statfs':
            st = os.statvfs(root)
            return {k: getattr(st, k) for k in ('f_bsize', 'f_frsize', 'f_blocks',
                    'f_bfree', 'f_bavail', 'f_files', 'f_ffree', 'f_favail', 'f_namemax')}
        path = args['path']
        guard = self.config.with_suffix('.fs-lock')
        with guard.open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX if op in ('write', 'truncate', 'create', 'mkdir', 'unlink', 'rmdir', 'rename') else fcntl.LOCK_SH)
            with self.parent(root, path) as (parent, name):
                if op == 'stat':return attributes(os.stat(name, dir_fd=parent, follow_symlinks=False))
                if op == 'list':
                    fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
                    try:return {'names': sorted(os.listdir(fd))}
                    finally:os.close(fd)
                if name == '.' and op not in ('stat', 'list'):raise OSError(errno.EPERM, 'Cannot mutate export root')
                if op in ('mkdir', 'unlink', 'rmdir', 'rename'):
                    if op == 'mkdir':os.mkdir(name, 0o700, dir_fd=parent)
                    elif op == 'unlink':os.unlink(name, dir_fd=parent)
                    elif op == 'rmdir':os.rmdir(name, dir_fd=parent)
                    else:
                        with self.parent(root, args['destination']) as (dest, target):
                            if target == '.':raise OSError(errno.EPERM, 'Cannot replace root')
                            os.rename(name, target, src_dir_fd=parent, dst_dir_fd=dest);os.fsync(dest)
                    os.fsync(parent);return {}
                if op not in ('read', 'write', 'truncate', 'create'):raise ValueError('Unknown filesystem operation')
                flags = os.O_RDONLY if op == 'read' else os.O_RDWR
                if op == 'create':flags |= os.O_CREAT | os.O_EXCL
                fd = os.open(name, flags | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600, dir_fd=parent)
                try:
                    st = os.fstat(fd)
                    if not stat.S_ISREG(st.st_mode):raise OSError(errno.EINVAL, 'Regular files only')
                    if op != 'create' and args.get('version') != version(st):raise OSError(errno.ESTALE, 'File changed; reopen')
                    if op == 'read':
                        offset, length = args['offset'], args['length']
                        if type(offset) is not int or type(length) is not int or offset < 0 or not 0 <= length <= BLOCK:raise ValueError('Invalid read range')
                        data = os.pread(fd, length, offset)
                        if version(os.fstat(fd)) != version(st):raise OSError(errno.ESTALE, 'File changed during read')
                        return {'data': base64.b64encode(data).decode(), 'sha256': hashlib.sha256(data).hexdigest()}
                    if op == 'write':
                        data = base64.b64decode(args['data'], validate=True)
                        offset = args['offset']
                        if len(data) > BLOCK or type(offset) is not int or offset < 0:raise ValueError('Invalid write range')
                        growth = max(0, offset+len(data)-st.st_size)
                    elif op == 'truncate':
                        if type(args['size']) is not int or args['size'] < 0:raise ValueError('Invalid file size')
                        growth = max(0, args['size']-st.st_size)
                    else:growth = 0
                    if shutil.disk_usage(root).free < growth+settings.get('minimum_free_bytes', 100*1024**3):raise OSError(errno.ENOSPC, 'Origin safety reserve')
                    if op == 'write':
                        count = os.pwrite(fd, data, offset)
                    elif op == 'truncate':os.ftruncate(fd, args['size'])
                    os.fsync(fd)
                    if op == 'create':os.fsync(parent)
                    return {**attributes(os.fstat(fd)), **({'written': count} if op == 'write' else {})}
                finally:os.close(fd)


def serve(config):
    import socketserver
    config = Path(config)
    digest = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()[:16]
    path = config.parent/('filesystem-runtime-'+digest+'.sock')
    lock = path.with_suffix('.lock').open('a')
    try:fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:return
    service = Filesystem(config.parent/'filesystem.json')
    class Handler(socketserver.StreamRequestHandler):
        def handle(self):
            self.request.settimeout(35)
            try:
                raw = self.rfile.readline(1024**2)
                if not raw.endswith(b'\n'):raise ValueError('Oversized filesystem request')
                value = json.loads(raw)
                result = {'ready': True} if value == {'probe': True} else service.dispatch(value)
                reply = {'ok': True, 'result': result}
            except Exception as error:reply = {'ok': False, 'error': str(error)[:200]}
            self.wfile.write(canonical(reply)+b'\n')
    class Server(socketserver.ThreadingUnixStreamServer):
        daemon_threads = True
    path.unlink(missing_ok=True)
    with Server(str(path), Handler) as server:
        path.chmod(0o600)
        pending = config.parent/'filesystem.sock.tmp';pending.unlink(missing_ok=True)
        pending.symlink_to(path.name);pending.replace(config.parent/'filesystem.sock')
        server.serve_forever()


class Proxy:
    def __init__(self, agent, source):
        self.agent = agent
        digest = hashlib.sha256(source.encode()).hexdigest()
        self.path = agent.config.parent/('filesystem-runtime-'+digest[:16]+'.sock')
        with (agent.config.parent/'filesystem-upgrade.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:self.call({'probe': True});return
            except OSError:pass
            folder = agent.config.parent/'command-runtimes';folder.mkdir(exist_ok=True, mode=0o700)
            script = folder/(digest+'.py')
            if script.exists() and script.read_text() != source:raise ValueError('Filesystem runtime changed')
            if not script.exists():
                pending = script.with_suffix('.tmp');pending.write_text(source);pending.chmod(0o600);pending.replace(script)
            # Reuse the same owner-capsule construction as persistent commands.
            runtime = __import__(agent.commands.__class__.__module__, fromlist=['CommandService'])
            owner = runtime.CommandService.__new__(runtime.CommandService)
            owner.agent = agent;owner.helper_lock = threading.Lock()
            owner.ensure_owner_helper(serve_script=script)
            deadline = time.monotonic()+20
            while True:
                try:self.call({'probe': True});break
                except OSError:
                    if time.monotonic() > deadline:raise
                    time.sleep(.1)

    def call(self, value):
        import socket
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(60);connection.connect(str(self.path));connection.sendall(canonical(value)+b'\n')
            with connection.makefile('rb') as stream:raw = stream.readline(1024**2)
        reply = json.loads(raw)
        if not reply['ok']:raise PermissionError(reply['error'])
        return reply['result']
