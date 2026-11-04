#!/usr/bin/env python3
"""Headless real-WebRTC transfer, reseeding, resume, corruption and WASM acceptance."""
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import time

from playwright.sync_api import sync_playwright

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'scripts'))
import peer_archive

BASE=os.environ.get('PEER_TEST_URL','http://localhost:8789')
KEY=Path(os.environ.get('PEER_TEST_KEY_FILE',ROOT/'peer-worker/.dev.vars'))
secret=KEY.read_text().strip()
if KEY.name=='.dev.vars':secret=next(x.split('=',1)[1] for x in secret.splitlines() if x.startswith('PEER_SIGNING_KEY='))

with tempfile.TemporaryDirectory(prefix='peer-browser-') as folder, sync_playwright() as p:
    folder=Path(folder);source=folder/'fixture.json';body=json.dumps({'fixture':'Synthetic test data','records':os.urandom(600000).hex()}).encode();source.write_bytes(body)
    package=folder/'package';room=peer_archive.package_file(source,package,'testing')
    browser=p.chromium.launch(executable_path='/usr/bin/google-chrome',headless=True,args=['--no-sandbox'])
    pages=[];contexts=[]
    def device(room=room,ui=False):
        context=browser.new_context();contexts.append(context);page=context.new_page();pages.append(page)
        errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
        page.goto(BASE,wait_until='networkidle');_,token=peer_archive.ticket(secret,room)
        if ui:
            page.get_by_label('Device invitation').fill(token);page.get_by_role('button',name='Connect device',exact=True).click()
            page.wait_for_function("()=>!document.querySelector('#package').disabled")
            return page,errors
        page.evaluate('''async token=>{
          const {ArchivePeer}=await import('/peer.js');window.events=[];
          window.peer=new ArchivePeer({url:location.origin,token});
          peer.addEventListener('error',e=>events.push(e.detail));await peer.connect();
        }''',token)
        return page,errors
    a,ae=device();b,be=device();c,ce=device()
    a.wait_for_function("()=>Array.from(peer.links.values()).filter(l=>l.dc?.readyState==='open').length===2")
    a.evaluate("document.body.insertAdjacentHTML('beforeend','<input id=seed type=file webkitdirectory multiple>')")
    a.locator('#seed').set_input_files(package)
    a.evaluate("async()=>{await peer.seed(document.querySelector('#seed').files)}")
    b.wait_for_function('()=>!!peer.manifest');c.wait_for_function('()=>!!peer.manifest')
    b.evaluate('async()=>{await peer.sync()}')
    actual=b.evaluate("async()=>{const b=await (await peer.file('testing-copy.json')).arrayBuffer();const {sha256}=await import('/peer.js');return {hash:await sha256(b),size:b.byteLength}}")
    assert actual=={'hash':hashlib.sha256(body).hexdigest(),'size':len(body)},actual
    print('PASS verified real WebRTC transfer and persisted file bytes',flush=True)
    a.close();c.wait_for_function("()=>Array.from(peer.links.values()).filter(l=>l.dc?.readyState==='open').length===1")
    c.evaluate('async()=>{await peer.sync()}')
    assert c.evaluate("async()=>{return (await peer.file('testing-copy.json')).size}")==len(body)
    print('PASS second device reseeds with original device offline',flush=True)
    # Corrupted local chunks are fetched again from the surviving complete peer.
    b.evaluate('async()=>{const [hash,size]=peer.parts.entries().next().value;await peer.store.put(hash,new Uint8Array(size));await peer.sync()}')
    assert b.evaluate("async()=>{return (await peer.file('testing-copy.json')).size}")==len(body)
    print('PASS resumable repair of corrupted local chunks',flush=True)
    # The WASM adapter reads the same verified bytes, without a production connection.
    result=b.evaluate('''async()=>{
      const files={};await peer.mountWasm({mkdirTree(){},writeFile(path,bytes){files[path]=bytes}});
      const name=Array.from(new TextEncoder().encode('fixtureSize'));
      const binary=new Uint8Array([0,97,115,109,1,0,0,0,1,5,1,96,0,1,127,2,19,1,3,101,110,118,11,...name,0,0,7,7,1,3,114,117,110,0,0]);
      const {instance}=await WebAssembly.instantiate(binary,{env:{fixtureSize:()=>files['/fixtures/testing-copy.json'].length}});
      return instance.exports.run();
    }''')
    assert result==len(body),result
    print('PASS browser/WASM consumes verified fixture',flush=True)
    c.close()
    # Malicious transfer is rejected before bytes enter the chunk store.
    b.evaluate('''()=>{peer.originalSend=peer.sendBytes;peer.sendBytes=async function(link,hash,bytes){
      if(hash!=='manifest'){bytes=bytes.slice();bytes[0]^=1;}return this.originalSend(link,hash,bytes);
    }}''')
    d,de=device();d.wait_for_function('()=>!!peer.manifest')
    rejected=d.evaluate('''async()=>{try{await peer.sync();return false;}catch{return true;}}''')
    assert rejected
    assert d.evaluate('async()=>{const hash=peer.parts.keys().next().value;return await peer.store.get(hash)===null}')
    print('PASS corrupt received bytes rejected before persistence',flush=True)
    b.evaluate('()=>{peer.sendBytes=peer.originalSend}')
    d.close()
    isolated,ie=device('b'*64)
    isolated.wait_for_timeout(500)
    assert isolated.evaluate('peer.links.size')==0
    assert isolated.evaluate('peer.manifest') is None
    print('PASS dataset room isolation',flush=True)
    ui,ue=device(ui=True)
    ui.wait_for_function("()=>document.querySelector('#dataset').textContent.includes('testing')")
    ui.get_by_role('button',name='Download or resume',exact=True).click()
    ui.wait_for_function("()=>document.querySelector('#status').textContent.includes('Complete dataset verified')")
    ui.get_by_role('button',name='Delete this device’s stored copy',exact=True).click()
    ui.wait_for_function("()=>document.querySelector('#dataset').textContent==='Local copy deleted.'")
    assert not any([ae,be,ce,de,ie,ue]),[ae,be,ce,de,ie,ue]
    print('PASS browser join, download and deletion controls',flush=True)
    invalid=b.evaluate('''async()=>{const {validateManifest}=await import('/peer.js');let rejected=0;
      for(const m of [{version:1,kind:'testing',expires:1,files:[]},{version:1,kind:'testing',expires:2147483647,files:[{path:'../outside',size:0,chunks:[]}]}]){
       try{validateManifest(m)}catch{rejected++}
      }return rejected}''')
    assert invalid==2
    print('PASS expired data and unsafe file paths rejected',flush=True)
    browser.close()
