"""Public snapshots use the same verified WebRTC transfer, room and expiry rules."""
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import time
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'scripts'))
from peer_archive import package_file, ticket
from public_data import FIELDS

base = os.environ['PEER_TEST_URL']
secret = Path(os.environ['PEER_TEST_KEY_FILE']).read_text().strip()
with tempfile.TemporaryDirectory(prefix='public-peer-') as folder, sync_playwright() as p:
    folder = Path(folder)
    body = json.dumps({'version': 3, **{name: [] for name in FIELDS}}).encode()
    source = folder / 'snapshot.json'; source.write_bytes(body)
    package = folder / 'package'; room = package_file(source, package, 'public', int(time.time()) + 3600)
    browser = p.chromium.launch(executable_path='/usr/bin/google-chrome', headless=True, args=['--no-sandbox'])
    try:
        pages = []
        for _ in range(2):
            page = browser.new_context().new_page(); page.goto(base, wait_until='networkidle')
            _, token = ticket(secret, room)
            page.evaluate('''async token => { const {ArchivePeer}=await import('/peer.js');
              window.peer=new ArchivePeer({url:location.origin,token});await peer.connect(); }''', token)
            pages.append(page)
        a, b = pages
        a.wait_for_function("()=>Array.from(peer.links.values()).some(link=>link.dc?.readyState==='open')")
        a.evaluate("document.body.insertAdjacentHTML('beforeend','<input id=seed type=file webkitdirectory multiple>')")
        a.locator('#seed').set_input_files(package)
        a.evaluate("async()=>peer.seed(document.querySelector('#seed').files)")
        b.wait_for_function("()=>peer.manifest?.kind==='public'")
        b.evaluate('async()=>peer.sync()')
        digest = b.evaluate("async()=>{const {sha256}=await import('/peer.js');return sha256(await (await peer.file('public-snapshot.json')).arrayBuffer())}")
        assert digest == hashlib.sha256(body).hexdigest()
        assert b.evaluate("async()=>{try{await peer.mountWasm({});return false}catch{return true}}")
        b.evaluate('async()=>peer.forget()')
        assert b.evaluate('peer.manifest') is None
        print('PASS public package transfers over real WebRTC, verifies bytes and deletes local state', flush=True)
    finally:
        browser.close()
