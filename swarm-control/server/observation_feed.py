"""Single-flight observation refreshes; request threads only read published data."""
import gzip
import hashlib
import json
import logging
import threading
import time


class SnapshotWarming(RuntimeError):
    pass


class ObservationFeed:
    def __init__(self, producer, interval=2, stable_revision=False):
        self.producer = producer
        self.interval = interval
        self.stable_revision = stable_revision
        self.lock = threading.Lock()
        self.sample = None
        self.revision = 0
        self.digest = None
        self.body = self.compressed = None
        self.busy = False
        self.attempted = None
        self.error = False

    def refresh(self):
        with self.lock:
            if self.busy or self.attempted is not None and time.monotonic()-self.attempted < self.interval:
                return
            self.busy = True
            self.attempted = time.monotonic()
        threading.Thread(target=self._refresh, daemon=True, name='observation-refresh').start()

    def _refresh(self):
        try:
            sample = self.producer()
            body = json.dumps(sample, separators=(',', ':'), allow_nan=False).encode()
            stable = json.dumps({k: v for k, v in sample.items() if k != 'sampled_at'},
                                sort_keys=True, separators=(',', ':'), allow_nan=False).encode() if self.stable_revision else body
            digest = hashlib.sha256(stable).digest()
            compressed = gzip.compress(body, compresslevel=1, mtime=0)
            with self.lock:
                if digest != self.digest:
                    self.revision += 1
                self.digest = digest
                self.sample, self.body, self.compressed = sample, body, compressed
                self.error = False
        except Exception:
            logging.exception('Observation refresh failed; retaining previous sample')
            with self.lock:
                self.error = True
        finally:
            with self.lock:
                self.busy = False

    def get(self):
        self.refresh()
        with self.lock:
            if self.sample is None:
                raise SnapshotWarming('Observation snapshot is warming')
            return self.revision, self.sample

    def response(self, accept_encoding=''):
        self.refresh()
        compressed = accepts_gzip(accept_encoding)
        with self.lock:
            if self.sample is None:
                raise SnapshotWarming('Observation snapshot is warming')
            return (self.compressed if compressed else self.body, compressed,
                    self.sample.get('sampled_at'), self.error)


def accepts_gzip(header):
    for value in header.lower().split(','):
        parts = [part.strip() for part in value.split(';')]
        if parts[0] != 'gzip':
            continue
        try:
            return all(float(part[2:]) > 0 for part in parts[1:] if part.startswith('q='))
        except ValueError:
            return False
    return False
