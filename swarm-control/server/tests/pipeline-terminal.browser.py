"""Isolated host-browser check of the terminal lifecycle."""
import json
import mimetypes
from pathlib import Path
from urllib.parse import urlsplit
from playwright.sync_api import sync_playwright
with sync_playwright() as p:
    browser=p.chromium.launch(headless=True,channel="chrome")
    page=browser.new_page()
    page.set_default_timeout(10000)
    page.on("pageerror",lambda error:print("Page error:",error,flush=True))
    calls=[]
    view=Path(__file__).resolve().parents[1]/'view'
    def assets(route):
        name=urlsplit(route.request.url).path.lstrip('/')
        path=view/name
        if path.is_file() and path.resolve().is_relative_to(view.resolve()):route.fulfill(body=path.read_bytes(),content_type=mimetypes.guess_type(name)[0] or 'application/octet-stream')
        else:route.fulfill(json={})
    page.route('http://localhost/**',assets)
    item={'id':'test/log-tail','job_id':'log-tail','parent':'Terminal test','name':'Terminal lifecycle fixture','status':'running','worker':'compai.local','kind':'regional','run_type':'genome','resources':{'cpus':1,'memory_mb':256},'runtime':{}}
    page.route('**/api/tasks/pipeline',lambda route:route.fulfill(json={'items':[item],'machines':['compai.local'],'errors':[]}))
    page.route('**/api/tasks/logs?*',lambda route:(calls.append(route.request.url),route.fulfill(json={'available':True,'text':'<script>bad()</script>\nworker output','running':True})))
    page.goto('http://localhost/pipeline.html',wait_until='domcontentloaded')
    page.locator('.shard[data-id="test/log-tail"]').click()
    terminal=page.locator('.shard-terminal')
    terminal.wait_for();assert calls==[]
    terminal.locator('summary').click()
    page.wait_for_function("document.querySelector('.terminal-output').textContent.includes('worker output')")
    assert len(calls)==1
    assert page.locator('.terminal-output script').count()==0
    # Repeated dashboard refresh must retain the same expanded disclosure.
    page.wait_for_timeout(2500)
    assert terminal.evaluate('(e)=>e.open')
    terminal.locator('summary').click();count=len(calls);page.wait_for_timeout(2300);assert len(calls)==count
    terminal.locator('summary').click();page.wait_for_timeout(300);assert len(calls)==count+1
    page.locator('.inspector-close').click();count=len(calls);page.wait_for_timeout(2300);assert len(calls)==count
    browser.close()
    print(json.dumps({'passed':True,'checks':['lazy connection','polling','safe text rendering','refresh retains expansion','collapse disconnects','reopen reconnects','close disconnects']}))
