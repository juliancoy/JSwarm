"""Read-only mmap view of a published memory object, independent of its format.

The existing private filesystem supplies authenticated peer cache RPCs; its
mount and running readers are never replaced. The bulk service supplies bounded
authenticated original/replica ranges. This process owns no controller key.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import errno
import json
import logging
import os
from pathlib import Path
import stat
import sys
import time

PROJECT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(PROJECT), str(PROJECT/'swarm-control/server')]
from scripts.swarm_memory import PageReader, validate
from scripts.run_input_cache import InputCache
from scripts.swarm_fs_mount import Client, select_system_fuse
from scripts.swarm_immutable_read import Reader, IntegrityError


class ObjectView:
    def __init__(self, config, *, cache=None, peer_client=None, transport=None):
        self.config = config; self.manifest = validate(config['object'])
        self.last_error = None
        settings = config['filesystem']
        self.client = peer_client or Client(settings)
        if cache is None:
            # All object mounts share one bounded tmpfs tier, distinct from
            # input staging. Never create a payload cache on a disk filesystem.
            root = Path('/dev/shm')/('swarm-memory-'+str(os.getuid()))
            root.mkdir(mode=0o700, exist_ok=True)
            if root.is_symlink() or root.stat().st_uid != os.getuid():
                raise ValueError('Unsafe memory cache ownership')
            root.chmod(0o700)
            memory = {line.split(':')[0]: int(line.split()[1])*1024
                      for line in Path('/proc/meminfo').read_text().splitlines()
                      if line.split()[1].isdigit()}
            budget = min(config.get('cache_bytes',256*1024**2),memory['MemAvailable']//16)
            cache = InputCache(root, budget=max(0,budget), reserve=128*1024**2)
        self.transport = transport or Reader({str(i): source for i, source in enumerate(config['sources'])})
        for source in config['sources']:
            if source['sha256'] != self.manifest['sha256'] or source['bytes'] != self.manifest['bytes']:
                raise ValueError('Memory source differs from published object')
        if not config['sources']: raise ValueError('An authoritative memory object source is required')
        self.pages = PageReader(self.manifest, cache, self.fetch, self.client.call,
                                settings.get('peers', ()), namespace=settings['namespace'],
                                replicas=config.get('replicas', 2))
        # ctypes FUSE callbacks may recreate Python thread-local state on each
        # invocation. Keep TLS reads on persistent Python workers so Reader's
        # per-thread authenticated connections survive consecutive page faults.
        self.transfers = ThreadPoolExecutor(max_workers=8, thread_name_prefix='memory-range')

    def close(self):
        self.pages.close()
        self.transfers.shutdown(wait=True, cancel_futures=True)

    def fetch(self, offset, length):
        failures = []
        for i in range(len(self.config['sources'])):
            # One retry refreshes a failed persistent connection and resolves
            # live addresses again; an unavailable path does not condemn a node.
            for attempt in range(2):
                try: return self.transfers.submit(self.transport.read, str(i), offset, length).result()
                except IntegrityError as error:
                    self.last_error = str(error)
                    self.pages.stats['source_integrity_errors'] += 1
                    raise
                except (OSError, RuntimeError) as error:
                    self.last_error = str(error)
                    self.pages.stats['source_transport_errors'] += 1
                    failures.append(str(error))
        raise OSError(errno.EHOSTUNREACH, 'Memory sources unavailable: '+ '; '.join(failures)[-300:])

    def metrics(self):
        return json.dumps({'object': self.manifest['id'], 'bytes': self.manifest['bytes'],
                           'page_bytes': self.manifest['page_bytes'], 'cache_bytes': self.pages.cache.budget,
                           'counters': dict(self.pages.stats),
                           'last_source_error': self.last_error,
                           'transport': dict(getattr(self.transport,'stats',{}))}, sort_keys=True).encode() + b'\n'


def mount(config, mountpoint, foreground=True):
    select_system_fuse(config['filesystem'])
    from mfusepy import FUSE, FuseOSError, Operations
    view = ObjectView(config)

    class Files(Operations):
        def getattr(self, path, fh=None):
            if path == '/': mode, size = stat.S_IFDIR | 0o500, 0
            elif path == '/object': mode, size = stat.S_IFREG | 0o400, view.manifest['bytes']
            elif path == '/metrics.json': mode, size = stat.S_IFREG | 0o400, len(view.metrics())
            else: raise FuseOSError(errno.ENOENT)
            return dict(st_mode=mode, st_size=size, st_nlink=2 if path=='/' else 1,
                        st_uid=os.getuid(), st_gid=os.getgid(), st_mtime=0, st_ctime=0, st_atime=0)

        def readdir(self, path, fh): return ['.', '..', 'object', 'metrics.json']

        def open(self, path, fi):
            self.getattr(path)
            if fi.flags & os.O_ACCMODE != os.O_RDONLY: raise FuseOSError(errno.EROFS)
            fi.direct_io = path != '/object'; fi.keep_cache = path == '/object'; fi.fh = 0
            return 0

        def read(self, path, size, offset, fh):
            try:
                if path == '/metrics.json': return view.metrics()[offset:offset+size]
                if path != '/object': raise OSError(errno.ENOENT, 'Unknown memory object')
                return view.pages.read(offset, size)
            except (OSError, ValueError, RuntimeError) as error:
                view.pages.stats['read_errors'] += 1
                logging.error('Memory object read failed at offset %d length %d: %s', offset, size, error)
                raise FuseOSError(getattr(error, 'errno', None) or errno.EIO)

        def statfs(self, path):
            return dict(f_bsize=4096, f_frsize=4096, f_blocks=(view.manifest['bytes']+4095)//4096,
                        f_bfree=0, f_bavail=0, f_files=3, f_ffree=0, f_namemax=255)

    try:
        FUSE(Files(), str(mountpoint), foreground=foreground, nothreads=False, ro=True,
             raw_fi=True, fsname='swarm-memory-'+view.manifest['id'], default_permissions=True,
             allow_root=config['filesystem'].get('allow_root', True) and not config['filesystem'].get('allow_other', False),
             allow_other=config['filesystem'].get('allow_other', False))
    finally: view.close()


if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',type=Path,required=True)
    parser.add_argument('mountpoint',type=Path)
    args=parser.parse_args()
    mount(json.loads(args.config.read_text()),args.mountpoint)
