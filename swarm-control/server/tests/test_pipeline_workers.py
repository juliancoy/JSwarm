from concurrent.futures import Future
import json

import pipeline_workers as module


def test_cache_projection_excludes_compute_and_secrets():
    status = {'node': 'mini', 'workers': [
        {'id': 'cache', 'job': 'job', 'group': 'shared-cache', 'active': True,
         'container': 'cache-container', 'cpus': .1, 'memory_mb': 256,
         'resident_mb': 64, 'command': 'secret'},
        {'id': 'compute', 'group': 'alignment', 'active': True},
        {'id': 'stopped', 'group': 'shared-cache', 'active': False}]}
    rows = module.project('mini', status, 123)
    assert len(rows) == 1
    assert rows[0]['resources'] == {'cpus': .1, 'memory_mb': 256}
    assert rows[0]['resident_mb'] == 64
    assert rows[0]['updated'] == 123
    assert 'secret' not in json.dumps(rows)


def test_snapshot_single_flight_expiry_and_enrollment(tmp_path, monkeypatch):
    config = tmp_path / 'peers.json'
    config.write_text(json.dumps({'peers': {'mini': '10.0.0.2'}}))
    monkeypatch.setattr(module, 'CONFIG', config)
    monkeypatch.setattr(module, 'CACHE', {})
    pending = []

    class Pool:
        def submit(self, *args):
            pending.append(args)
            return Future()

    monkeypatch.setattr(module, 'POOL', Pool())
    monkeypatch.setattr(module.time, 'monotonic', lambda: 100)
    assert module.snapshot()['items'] == []
    module.snapshot()
    assert len(pending) == 1  # Requests never wait on or duplicate fleet calls.
    module.CACHE['mini'].update(items=[{'id': 'cache'}], observed=100)
    assert module.snapshot()['items'] == [{'id': 'cache'}]
    monkeypatch.setattr(module.time, 'monotonic', lambda: 161)
    assert module.snapshot()['items'] == []  # Old observations are not live work.
    config.write_text(json.dumps({'peers': {}}))
    assert module.snapshot()['items'] == []
