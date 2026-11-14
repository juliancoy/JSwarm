"""Public, bounded deployment observations; never publish configuration or logs."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import threading
import time
import uuid

_active = None


class Progress:
    def __init__(self, config, operation):
        self.folder = Path(config).parent / 'deployment-progress'
        self.folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.path = self.folder / (uuid.uuid4().hex + '.json')
        self.lock = threading.RLock()
        self.stop = threading.Event()
        self.value = dict(id=self.path.stem, operation=operation, status='queued',
                          queued_at=time.time(), nodes=[], revision=None)
        self.update()

    def update(self, **values):
        with self.lock:
            self.value.update(values, updated_at=time.time())
            temporary = self.path.with_suffix('.tmp')
            temporary.write_text(json.dumps(self.value))
            temporary.chmod(0o600)
            temporary.replace(self.path)

    def heartbeat(self):
        while not self.stop.wait(2):
            self.update()


def update(**values):
    if _active:
        _active.update(**values)


def node(name, status, **values):
    if _active:
        with _active.lock:
            rows = [dict(row) for row in _active.value['nodes']]
            row = next((row for row in rows if row['node'] == name), None)
            if row is None:
                row = {'node': name}
                rows.append(row)
            row.update(status=status, **values)
            _active.update(nodes=rows)


@contextmanager
def observe(config, operation):
    global _active
    if _active is not None:
        yield _active
        return
    progress = Progress(config, operation)
    _active = progress
    thread = threading.Thread(target=progress.heartbeat, daemon=True)
    thread.start()
    try:
        yield progress
    except BaseException as error:
        progress.update(status='failed', error=type(error).__name__, finished_at=time.time())
        raise
    else:
        progress.update(status='failed' if progress.value.get('status') == 'failed' else 'complete', finished_at=time.time())
    finally:
        progress.stop.set()
        thread.join(timeout=3)
        _active = None
        # Retain recent completed attempts while preserving all live requests.
        completed = []
        for path in progress.folder.glob('*.json'):
            try:
                value = json.loads(path.read_text())
                if value.get('status') in ('complete', 'failed'):
                    completed.append(path)
            except (OSError, ValueError):
                continue
        for path in sorted(completed, key=lambda p: p.stat().st_mtime, reverse=True)[20:]:
            path.unlink(missing_ok=True)
