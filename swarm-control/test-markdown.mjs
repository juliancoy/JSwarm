// Isolated synthetic messages; no account session or production conversation reads.
import {createServer} from 'node:http';
import {readFile,mkdir} from 'node:fs/promises';
import {resolve,join} from 'node:path';
import {fileURLToPath} from 'node:url';
import assert from 'node:assert/strict';
import {chromium} from '@playwright/test';
const root=resolve(fileURLToPath(new URL('server/view/',import.meta.url))),output=resolve(fileURLToPath(new URL('../artifacts/swarm-markdown/',import.meta.url)));await mkdir(output,{recursive:true});
const server=createServer(async(req,res)=>{
 const path=new URL(req.url,'http://localhost').pathname;
 if(path==='/'){res.setHeader('Content-Type','text/html');res.end('<!doctype html><meta name="viewport" content="width=device-width,initial-scale=1"><link rel="stylesheet" href="/codex.css"><style>body{margin:0;padding:20px;background:#202020;color:#eee;font-family:Arial}main{max-width:760px;margin:auto}.codex-message{width:100%}</style><main></main>');return;}
 if(!['/codex.css','/message-markdown.js','/marked-vendor.js'].includes(path)){res.writeHead(404);res.end();return;}
 res.setHeader('Content-Type',path.endsWith('.js')?'text/javascript':'text/css');res.end(await readFile(join(root,path)));
});await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
const origin='http://127.0.0.1:'+server.address().port;
const browser=await chromium.launch({headless:true,executablePath:process.env.CHROME_PATH||'/usr/bin/google-chrome'});
try{
 for(const width of [1440,390]){
 const context=await browser.newContext({viewport:{width,height:1000},permissions:['clipboard-read','clipboard-write']});const page=await context.newPage();const errors=[];page.on('pageerror',e=>errors.push(e.message));await page.goto(origin);
 await page.evaluate(async()=>{
  const {renderMessageMarkdown}=await import('/message-markdown.js');
  const text='# Swarm results\n\n**Six computers** reported *live* measurements. ~~Old gap~~ fixed. &amp; &lt; stays readable.\n\n| Computer | Status |\n| --- | --- |\n| CompAI | **Passed** |\n| MiniPC | Running |\n\n1. Check telemetry\n   - Preserve partial updates\n2. Review history\n\n- [x] CPU check\n- [ ] GPU check\n\n> Measurements cover the last five minutes.\n\n```python\nprint("<script>literal</script>")\n```\n\nUse `sampled_at` and [Task Manager](/task-manager).\n\n[Unsafe](javascript:alert(1)) [Encoded unsafe](jav&#x61;script:alert(1))\n\n<img src=x onerror="window.markdownExecuted=true">\n\n[Local file](/home/julian/test.py:12)\n\n![External image](https://invalid.example/image.png)';
  const message=document.createElement('article');message.className='codex-message assistant';message.append(renderMessageMarkdown(text));document.querySelector('main').append(message);
 });
 assert.equal(await page.locator('h1').textContent(),'Swarm results');assert.equal(await page.locator('table tbody tr').count(),2);assert.equal(await page.locator('ol>li').count(),2);assert.equal(await page.locator('input:checked').count(),1);assert.equal(await page.locator('blockquote').count(),1);assert.equal(await page.locator('.message-task').first().textContent(),'CPU check');
 assert.equal(await page.locator('a[href^="javascript:"]').count(),0);assert.equal(await page.locator('img,script:not([src])').count(),0);assert.equal(await page.evaluate(()=>window.markdownExecuted),undefined);assert.equal(await page.locator('a[href*="/home/julian"]').count(),0);
 assert.ok((await page.locator('main').textContent()).includes('& < stays readable.'));assert.equal(await page.locator('pre code').textContent(),'print("<script>literal</script>")');
 await page.getByRole('button',{name:'Copy code'}).click();assert.equal(await page.evaluate(()=>navigator.clipboard.readText()),'print("<script>literal</script>")');
 assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);assert.deepEqual(errors,[]);
 await page.screenshot({path:join(output,`markdown-${width}.png`),fullPage:true});
 // Streaming/unfinished fences should render as code, not break the conversation.
 await page.evaluate(async()=>{const {renderMessageMarkdown}=await import('/message-markdown.js');document.querySelector('main').replaceChildren(renderMessageMarkdown('```js\nconst x = 1'));});assert.equal(await page.locator('pre code').textContent(),'const x = 1');
 console.log('Markdown rendering, copy, safety, unfinished response and width checks passed:',width);await context.close();
 }
}finally{await browser.close();server.closeAllConnections();await new Promise(resolve=>server.close(resolve));}
