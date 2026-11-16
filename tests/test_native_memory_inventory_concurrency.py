"""A stalled cache peer must not serialize unrelated source-backed page faults."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import socket
import subprocess
import threading
import time

import pytest
from test_native_swarm_memory import BINARY, environment


def test_one_stalled_inventory_probe_does_not_block_other_faults(environment, tmp_path):
    if not Path('/dev/fuse').exists():
        pytest.skip('Host FUSE is unavailable')
    env = environment
    path, config = env['configs']['a.local']
    config['objects'][0]['peers'] = ['b.local']
    path.write_text(json.dumps(config))
    accepted, release = threading.Event(), threading.Event()
    listener = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
    listener.bind(('::1', env['ports']['b.local']))
    listener.listen()
    listener.settimeout(.2)

    def blackhole():
        while not release.is_set():
            try:connection, _ = listener.accept()
            except socket.timeout:continue
            except OSError:return
            accepted.set()
            release.wait(10)
            connection.close()

    thread = threading.Thread(target=blackhole, daemon=True)
    thread.start()
    log = (tmp_path/'native.log').open('w+')
    process = subprocess.Popen([str(BINARY), '--config', str(path)], stdout=log, stderr=log)
    mount = Path(config['objects'][0]['mountpoint'])
    try:
        deadline = time.monotonic()+10
        while not os.path.ismount(mount) and process.poll() is None and time.monotonic()<deadline:
            time.sleep(.05)
        log.seek(0)
        assert os.path.ismount(mount), log.read()

        def read(offset):
            with (mount/'object').open('rb', buffering=0) as f:
                return os.pread(f.fileno(), 4096, offset)

        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(read, 0)
            try:
                assert accepted.wait(5), 'First page must reach the stalled inventory peer'
                second = pool.submit(read, 1024*1024)
                assert second.result(timeout=2) == env['data'][1024*1024:1024*1024+4096]
            finally:
                release.set()
            assert first.result(timeout=10) == env['data'][:4096]
        counters = json.loads((mount/'metrics.json').read_text())['counters']
        assert counters['inventory_coalesced'] >= 1
    finally:
        release.set()
        listener.close()
        thread.join(timeout=2)
        process.terminate()
        try:process.wait(timeout=15)
        except subprocess.TimeoutExpired:process.kill();process.wait()
        log.close()
