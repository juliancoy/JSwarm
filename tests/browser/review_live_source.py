"""Exercise real browser caching and hot reload against NodePipelineWeb, on the host.

Run: PYTHONPATH=JSwarm:. python JSwarm/tests/browser/review_live_source.py
"""
import asyncio
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace

from aiohttp import web
from playwright.async_api import async_playwright
from scripts import launcher_peers as peers


async def main():
    with tempfile.TemporaryDirectory(prefix='jswarm-live-review-') as temporary:
        root=Path(temporary)
        view=root/'JSwarm/swarm-control/server/view';view.mkdir(parents=True)
        (root/'GenomeHarbor/web').mkdir(parents=True)
        (view/'jswarm-live.js').write_bytes((Path(__file__).parents[2]/'swarm-control/server/view/jswarm-live.js').read_bytes())
        html='<html><head><link rel="stylesheet" href="/style.css"><script type="module" src="/entry.js"></script></head><body><h1>original</h1><select id="composition-dataset"><option value="reference">Reference</option><option value="hg001">HG001</option></select><p id="version"></p><p id="data"></p></body></html>'
        (view/'index.html').write_text(html)
        (view/'style.css').write_text('body { color: rgb(10,20,30); }')
        (view/'entry.js').write_text("import {value} from './dependency.js';document.querySelector('#version').textContent=value;document.querySelector('#data').textContent=(await (await fetch('/data.json')).json()).value;")
        (view/'dependency.js').write_text('export const value="first";')
        (view/'data.json').write_text('{"value":"one"}')
        config=root/'peers.json';config.write_text(json.dumps({'name':'codecollective.local','peers':{'codecollective.local':'10.42.0.49','compai.local':'10.42.0.1'},'web_workspace':str(root)}))
        # Only this isolated harness strips its random loopback port; production
        # authority restrictions remain covered by test_node_web.py.
        original_target=peers.pipeline_target
        peers.pipeline_target=lambda config,authority:original_target(config,authority.split(':')[0])
        app=web.Application();app.router.add_route('*','/{path:.*}',peers.NodePipelineWeb(SimpleNamespace(config=config)).handle)
        runner=web.AppRunner(app);await runner.setup()
        site=web.TCPSite(runner,'127.0.0.1',0);await site.start()
        port=site._server.sockets[0].getsockname()[1]
        try:
            async with async_playwright() as p:
                browser=await p.chromium.launch(executable_path='/usr/bin/google-chrome',headless=True)
                page=await browser.new_page();errors=[];page.on('pageerror',lambda error:errors.append(str(error)))
                response=await page.goto(f'http://127.0.0.1:{port}/index.html#composition')
                assert 'no-store' in response.headers['cache-control']
                await page.wait_for_function("document.querySelector('#version').textContent==='first'")
                await page.locator('#composition-dataset').select_option('hg001')
                (view/'dependency.js').write_text('export const value="second";')
                await page.wait_for_function("document.querySelector('#version').textContent==='second'",timeout=12000)
                assert await page.locator('#composition-dataset').input_value()=='hg001'
                assert page.url.endswith('#composition')
                (view/'data.json').write_text('{"value":"two"}')
                await page.wait_for_function("document.querySelector('#data').textContent==='two'",timeout=12000)
                (view/'style.css').write_text('body { color: rgb(90,80,70); }')
                await page.wait_for_function("getComputedStyle(document.body).color==='rgb(90, 80, 70)'",timeout=12000)
                (view/'index.html').write_text(html.replace('original','fresh HTML'))
                await page.wait_for_function("document.querySelector('h1').textContent==='fresh HTML'",timeout=12000)
                assert await page.locator('#composition-dataset').input_value()=='hg001'
                assert not errors,errors
                print(json.dumps({'passed':True,'checks':['no-store response','imported JS reload','JSON reload','CSS reload','HTML reload','dataset and hash retained'],'errors':errors}))
                await browser.close()
        finally:
            peers.pipeline_target=original_target
            await runner.cleanup()


if __name__=='__main__':
    asyncio.run(main())
