#!/usr/bin/env python3
"""Run isolated real-WebRTC acceptance against a local peer Worker."""
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import tempfile
import time
import urllib.request

ROOT = (Path(__file__).resolve().parents[2] if Path(__file__).resolve().parents[1].name in ('HarborGenetics', 'JSwarm') else Path(__file__).resolve().parents[1])


def main():
    with tempfile.TemporaryDirectory(prefix='public-peer-browser-') as folder:
        folder = Path(folder)
        key = folder / 'signing.key'; key.write_text(secrets.token_hex(32)); key.chmod(0o600)
        config = json.loads((ROOT / 'peer-worker/wrangler.jsonc').read_text())
        config.update(main=str(ROOT / 'peer-worker/src/worker.ts'),
                      assets={'directory': str(ROOT / 'peer-worker/public'), 'binding': 'ASSETS', 'run_worker_first': True},
                      vars={'PEER_SIGNING_KEY': key.read_text(), 'PEER_ALLOWED_ORIGINS': 'http://localhost:18789'})
        path = folder / 'wrangler.json'; path.write_text(json.dumps(config)); path.chmod(0o600)
        env = {**os.environ, 'PEER_TEST_URL': 'http://localhost:18789', 'PEER_TEST_KEY_FILE': str(key), 'WRANGLER_SEND_METRICS': 'false'}
        with (folder / 'worker.log').open('w') as log:
            child = subprocess.Popen([str(ROOT / 'peer-worker/node_modules/.bin/wrangler'), 'dev', '--config', str(path),
                '--local', '--ip', '127.0.0.1', '--port', '18789', '--persist-to', str(folder / 'state')],
                cwd=folder, env=env, stdout=log, stderr=subprocess.STDOUT)
            try:
                for _ in range(90):
                    if child.poll() is not None:
                        raise RuntimeError('Local peer Worker exited: ' + (folder / 'worker.log').read_text()[-4000:])
                    try:
                        with urllib.request.urlopen(env['PEER_TEST_URL'] + '/health', timeout=1):
                            break
                    except OSError:
                        time.sleep(1)
                else:
                    raise RuntimeError('Local peer Worker did not become ready')
                for script in ['backup.py', 'browser.py', 'public-browser.py']:
                    subprocess.run([sys.executable, str(ROOT / 'peer-worker/test' / script)], env=env, check=True)
            finally:
                child.terminate()
                try: child.wait(timeout=15)
                except subprocess.TimeoutExpired: child.kill(); child.wait()


if __name__ == '__main__':
    main()
