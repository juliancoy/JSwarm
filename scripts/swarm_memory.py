"""Application-independent, immutable objects backed by bounded peer RAM pages.

Manifests are installed through the enrolled owner channel. Every page is checked
against that manifest, including local hits and authenticated peer hits. Losing
an evictable replica never loses the authoritative object. Mutable heaps are not
part of this interface.
"""
import base64
from collections import Counter
import errno
import hashlib
import json
import os
from pathlib import Path
import queue
import re
import stat
import threading
import time

SCHEMA = 'jswarm-memory-object.v1'
PAGE_BYTES = 64 * 1024
MAX_PAGE_BYTES = 256 * 1024


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def validate(manifest):
    if (manifest.get('schema') != SCHEMA or type(manifest.get('bytes')) is not int
            or manifest['bytes'] <= 0 or type(manifest.get('page_bytes')) is not int
            or not 4096 <= manifest['page_bytes'] <= MAX_PAGE_BYTES
            or manifest['page_bytes'] & (manifest['page_bytes'] - 1)):
        raise ValueError('Invalid immutable memory object')
    hashes = manifest.get('pages')
    if (not isinstance(hashes, list)
            or len(hashes) != (manifest['bytes'] + manifest['page_bytes'] - 1) // manifest['page_bytes']
            or any(not isinstance(h, str) or not re.fullmatch('[a-f0-9]{64}', h)
                   for h in [manifest.get('sha256'), *hashes])):
        raise ValueError('Invalid immutable page hashes')
    identity = {k: manifest[k] for k in ('schema', 'bytes', 'page_bytes', 'sha256', 'pages')}
    if manifest.get('id') != digest(identity):
        raise ValueError('Memory object manifest identity differs')
    return manifest


def seal(path, expected_sha256, page_bytes=PAGE_BYTES):
    """Read an already immutable original once; persist metadata, never payload."""
    if type(page_bytes) is not int or not 4096 <= page_bytes <= MAX_PAGE_BYTES or page_bytes & (page_bytes - 1):
        raise ValueError('Invalid memory page size')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_mode & 0o222 or before.st_size <= 0:
            raise ValueError('Publish a nonempty read-only regular file')
        whole = hashlib.sha256(); pages = []
        while True:
            data = os.read(fd, page_bytes)
            if not data: break
            # Regular-file short reads are legal; preserve fixed page boundaries.
            while len(data) < page_bytes:
                more = os.read(fd, page_bytes - len(data))
                if not more: break
                data += more
            whole.update(data); pages.append(hashlib.sha256(data).hexdigest())
        after = os.fstat(fd)
        identity = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns)
        if identity(before) != identity(after) or whole.hexdigest() != expected_sha256:
            raise ValueError('Published source changed or differs from expected digest')
        result = dict(schema=SCHEMA, bytes=before.st_size, page_bytes=page_bytes,
                      sha256=expected_sha256, pages=pages)
        result['id'] = digest(result)
        return validate(result)
    finally:
        os.close(fd)


def resolve_mount(deployment, object_id, node, machine):
    """Resolve a verified mount without coupling applications to cache transport.

    The caller owns the deployment receipt (installed and verified by the
    controller). A new transport release may expose the same immutable object;
    scientific qualification remains bound to its content, not its pathname.
    """
    if deployment.get('object_id') != object_id: return None
    row = deployment.get('nodes', {}).get(node, {})
    revision = row.get('revision', '')
    path = Path(row.get('path', ''))
    if (row.get('ready') is not True or row.get('machine') != machine
            or row.get('object_id') != object_id or not re.fullmatch('[a-f0-9]{64}', object_id)
            or not re.fullmatch('[a-f0-9]{64}', revision) or not path.is_absolute()
            or '..' in path.parts or path.parts[-4:] != ('SwarmMemory', object_id, revision, 'object')):
        return None
    return dict(row)


class PageReader:
    """Shared verified pages; transport and filesystem policy are injected.

    peer_call uses the existing authenticated filesystem cache RPC. fetch accepts
    a bounded (offset, length) and may try multiple enrolled immutable sources.
    Peer lookups have a total deadline; publication never delays a page fault.
    """
    def __init__(self, manifest, cache, fetch, peer_call, peers=(), *, namespace,
                 replicas=2, probe_seconds=.5, clock=time.monotonic):
        self.manifest = validate(manifest)
        self.cache, self.fetch, self.peer_call = cache, fetch, peer_call
        self.peers = tuple(sorted(set(peers)))
        self.namespace = namespace
        self.replicas = max(0, min(int(replicas), len(self.peers)))
        self.probe_seconds, self.clock = probe_seconds, clock
        self.stats = Counter(); self.cooldown = {}; self.misses = {}
        self.guard = threading.Lock()
        # Fixed stripes coalesce concurrent faults without an ever-growing map.
        self.locks = [threading.Lock() for _ in range(64)]
        self.pending = queue.Queue(maxsize=max(1, 4 * 1024**2 // manifest['page_bytes']))
        self.publisher = None

    def key(self, number):
        return digest([self.namespace, self.manifest['id'], number])[:32]

    def verified(self, data, number):
        length = min(self.manifest['page_bytes'], self.manifest['bytes'] - number * self.manifest['page_bytes'])
        return (isinstance(data, bytes) and len(data) == length
                and hashlib.sha256(data).hexdigest() == self.manifest['pages'][number])

    def ranked(self, key):
        return sorted(self.peers, key=lambda n: digest([key, n]), reverse=True)

    def publish(self, key, data):
        if not self.replicas: return
        with self.guard:
            if self.publisher is None:
                self.publisher = threading.Thread(target=self._publish, daemon=True, name='memory-replicas')
                self.publisher.start()
        try: self.pending.put_nowait((key, data))
        except queue.Full: self.stats['replication_backpressure'] += 1

    def _publish(self):
        while True:
            item = self.pending.get()
            try:
                if item is None: return
                key, data = item; stored = 0
                for peer in self.ranked(key):
                    if self.cooldown.get(peer, 0) > self.clock(): continue
                    try:
                        row = self.peer_call(peer, 'cache.put', timeout=.5, key=key,
                                             length=len(data), data=base64.b64encode(data).decode())
                        if row.get('stored'):
                            stored += 1; self.stats['replicas_stored'] += 1
                            if stored >= self.replicas: break
                        else: self.stats['replica_admission_denied'] += 1
                    except (OSError, RuntimeError, ValueError):
                        self.cooldown[peer] = self.clock() + 2
                        self.stats['peer_errors'] += 1
            finally: self.pending.task_done()

    def close(self):
        if self.publisher is not None:
            # Replication is opportunistic; do not block shutdown on a queue
            # of offline peers. Finish at most the currently executing item.
            while True:
                try:self.pending.get_nowait();self.pending.task_done()
                except queue.Empty:break
            self.pending.put_nowait(None)
            self.publisher.join(timeout=max(2, len(self.peers)))

    def page(self, number):
        with self.locks[number % len(self.locks)]:
            return self._page(number)

    def _page(self, number):
        key = self.key(number); size = self.manifest['page_bytes']
        start = number * size; length = min(size, self.manifest['bytes'] - start)
        data = self.cache.lookup(key, length, 0)
        if self.verified(data, number):
            self.stats['local_hits'] += 1
            return data
        if data is not None: self.stats['corrupt_local_pages'] += 1
        # Cache integrity includes the expected publisher hash, not just an
        # internally consistent checksum supplied by another node.
        deadline = self.clock() + self.probe_seconds
        ranked = self.ranked(key)
        for position, peer in enumerate(ranked):
            remaining = deadline - self.clock()
            if remaining <= 0: break
            if max(self.cooldown.get(peer, 0), self.misses.get((peer, key), 0)) > self.clock(): continue
            try:
                # Reserve an attempt for each alternate peer: a blackholed
                # first route must not consume the entire peer-read deadline.
                row = self.peer_call(peer, 'cache.get', timeout=remaining/(len(ranked)-position), key=key, length=length)
                candidate = base64.b64decode(row['data'], validate=True) if row.get('data') is not None else None
                if self.verified(candidate, number):
                    data = candidate; self.stats['peer_hits'] += 1; break
                if candidate is not None: self.stats['corrupt_peer_pages'] += 1
                self.stats['peer_misses'] += 1
                if len(self.misses) >= 4096:
                    now = self.clock()
                    self.misses = {k: t for k, t in self.misses.items() if t > now}
                    if len(self.misses) >= 4096: self.misses.clear()
                self.misses[(peer, key)] = self.clock() + 2
            except (OSError, RuntimeError, ValueError):
                self.cooldown[peer] = self.clock() + 2; self.stats['peer_errors'] += 1
        else: data = None
        # A loop interrupted by its time budget may still have the invalid
        # local bytes; only a publisher-verified page is ever returned.
        if not self.verified(data, number):
            data = self.fetch(start, length)
            if not self.verified(data, number):
                self.stats['integrity_failures'] += 1
                raise OSError(errno.EIO, 'Immutable source page hash differs')
            self.stats['source_reads'] += 1; self.stats['source_bytes'] += len(data)
        # InputCache may contain a self-consistent but wrong page. invalidate()
        # removes it under its own cache lock before admitting verified bytes.
        old = self.cache.lookup(key, length, 0)
        if old is not None and not self.verified(old, number): self.cache.invalidate(key, length, 0)
        self.cache.block(key, length, 0, lambda *_: data)
        self.publish(key, data)
        return data

    def read(self, offset, length):
        if type(offset) is not int or type(length) is not int or offset < 0 or length < 0:
            raise ValueError('Invalid memory object range')
        stop = min(offset + length, self.manifest['bytes']); result = bytearray()
        while offset < stop:
            number, within = divmod(offset, self.manifest['page_bytes'])
            data = self.page(number); count = min(stop - offset, len(data) - within)
            result.extend(data[within:within + count]); offset += count
        return bytes(result)
