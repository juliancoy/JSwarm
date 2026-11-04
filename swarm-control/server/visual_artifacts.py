"""Bounded PNG/JSON transfer through the launcher's existing worker event stream."""
import base64
import hashlib
import re
from pathlib import Path
from urllib.parse import urlencode

MAX_FILE = 4 * 1024 * 1024
MAX_TOTAL = 32 * 1024 * 1024
# 68 layout captures, four contact sheets and bounded JSON reports.
MAX_FILES = 96
CHUNK = 8192
NAME = re.compile(r'^[a-z0-9][a-z0-9._-]{0,120}\.(?:png|json)$')


def publish(folder, suite, emit):
    total = 0
    files = [path for path in sorted(Path(folder).glob('*')) if path.is_file() and NAME.fullmatch(path.name)]
    if len(files) > MAX_FILES:
        raise ValueError('Too many visual artifacts')
    for path in files:
        data = path.read_bytes()
        total += len(data)
        if len(data) > MAX_FILE or total > MAX_TOTAL:
            raise ValueError('Visual artifact size limit exceeded')
        digest = hashlib.sha256(data).hexdigest()
        emit('artifact_begin', suite=suite, name=path.name, size=len(data), sha256=digest)
        for sequence, offset in enumerate(range(0, len(data), CHUNK)):
            emit('artifact_chunk', suite=suite, name=path.name, sequence=sequence,
                 data=base64.b64encode(data[offset:offset+CHUNK]).decode())
        emit('artifact_end', suite=suite, name=path.name)


class VisualArtifacts:
    def __init__(self, folder):
        self.folder = Path(folder)
        self.pending = {}
        self.totals = {}

    def receive(self, run, host, suite, event):
        name = event.get('name', '')
        if not NAME.fullmatch(name):
            raise ValueError('Invalid visual artifact name')
        # Hash host names, never use peer-supplied path components.
        machine = hashlib.sha256(host.encode()).hexdigest()[:16]
        key = (run, machine, suite, name)
        budget = key[:3]
        directory = self.folder / (run + '-artifacts') / machine / suite
        kind = event.get('kind')
        if kind == 'artifact_begin':
            size, digest = event.get('size'), event.get('sha256', '')
            if not isinstance(size, int) or not 0 < size <= MAX_FILE or not re.fullmatch('[a-f0-9]{64}', digest):
                raise ValueError('Invalid visual artifact metadata')
            total, count = self.totals.get(budget, (0, 0))
            if total + size > MAX_TOTAL or count >= MAX_FILES or key in self.pending:
                raise ValueError('Visual artifact transfer limit exceeded')
            directory.mkdir(parents=True, exist_ok=True)
            partial = directory / (name + '.part')
            partial.write_bytes(b'')
            self.pending[key] = {'path': partial, 'size': size, 'sha256': digest, 'received': 0, 'sequence': 0}
            self.totals[budget] = (total + size, count + 1)
            return None
        state = self.pending.get(key)
        if not state:
            raise ValueError('Visual artifact transfer was not started')
        if kind == 'artifact_chunk':
            value = event.get('data', '')
            if not isinstance(value, str) or len(value) > 4 * ((CHUNK + 2) // 3) or event.get('sequence') != state['sequence']:
                raise ValueError('Invalid visual artifact chunk')
            data = base64.b64decode(value, validate=True)
            if len(data) > CHUNK or state['received'] + len(data) > state['size']:
                raise ValueError('Visual artifact exceeds declared size')
            with state['path'].open('ab') as stream:
                stream.write(data)
            state['received'] += len(data)
            state['sequence'] += 1
            return None
        if kind != 'artifact_end':
            raise ValueError('Unknown visual artifact event')
        self.pending.pop(key)
        data = state['path'].read_bytes()
        if len(data) != state['size'] or hashlib.sha256(data).hexdigest() != state['sha256']:
            state['path'].unlink(missing_ok=True)
            raise ValueError('Visual artifact checksum mismatch')
        if name.endswith('.png') and not data.startswith(b'\x89PNG\r\n\x1a\n'):
            state['path'].unlink(missing_ok=True)
            raise ValueError('Invalid visual PNG')
        state['path'].replace(directory / name)
        query = urlencode({'run': run, 'host': host, 'suite': suite, 'name': name})
        return {'name': name, 'size': len(data), 'sha256': state['sha256'],
                'url': '/api/distributed-tests/artifacts?' + query}

    def read(self, run, host, suite, name):
        if not re.fullmatch(r'[a-zA-Z0-9_-]{1,100}', run) or suite not in ('visual','mobile') or not NAME.fullmatch(name):
            raise ValueError('Invalid visual artifact path')
        machine = hashlib.sha256(host.encode()).hexdigest()[:16]
        path = self.folder / (run + '-artifacts') / machine / suite / name
        if not path.is_file() or path.stat().st_size > MAX_FILE:
            raise FileNotFoundError(name)
        return path.read_bytes(), 'image/png' if name.endswith('.png') else 'application/json'
