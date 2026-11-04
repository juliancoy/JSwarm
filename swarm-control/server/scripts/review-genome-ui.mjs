import {createServer} from 'node:http';
import {readFile,mkdir} from 'node:fs/promises';
import {fileURLToPath} from 'node:url';
import {join} from 'node:path';
import {createRequire} from 'node:module';
import assert from 'node:assert/strict';
const require=createRequire(new URL('../../package.json',import.meta.url));
const {chromium,expect}=require('@playwright/test');
const view=fileURLToPath(new URL('../view/',import.meta.url));
const output=process.argv[2]||'/tmp/genome-interactions';await mkdir(output,{recursive:true});
const names=new Set(['genome-explorer.html','genome-explorer.js','genome-explorer.css','genome-data.js','swarm-control.svg']);
const server=createServer(async(req,res)=>{const name=new URL(req.url,'http://localhost').pathname.slice(1);if(!names.has(name)){res.writeHead(404).end();return;}try{const content=await readFile(join(view,name));res.setHeader('Content-Type',name.endsWith('.js')?'application/javascript':name.endsWith('.css')?'text/css':name.endsWith('.svg')?'image/svg+xml':'text/html');res.end(content);}catch{res.writeHead(500).end();}});
await new Promise(r=>server.listen(0,'127.0.0.1',r));
const browser=await chromium.launch({executablePath:'/usr/bin/google-chrome',headless:true});
try{
 for(const width of [390,1440]){
  const context=await browser.newContext({viewport:{width,height:1000}});const page=await context.newPage();const errors=[];page.on('pageerror',e=>errors.push(e.message));
  await page.goto(`http://127.0.0.1:${server.address().port}/genome-explorer.html`);
  await page.locator('.genome-rings').waitFor();
  await page.locator('.primary-action').click();assert.equal(new URL(page.url()).hash,'#overview');
  assert.match(await page.locator('#overview-takeaway').innerText(),/All six datasets/);
  await page.locator('.journey a[href="#satellites"]').click();await expect(page.locator('.journey a[href="#satellites"]')).toHaveAttribute('aria-current','step');
  await page.locator('.journey a[href="#next-step"]').click();await expect(page.locator('.journey a[href="#next-step"]')).toHaveAttribute('aria-current','step');
  await page.locator('#compare-genomes').click();
  assert.equal(await page.locator('#sample').count(),0);
  assert.equal(await page.locator('#dataset-legend .dataset-key').count(),6);
  assert.equal(await page.locator('.genome-rings g[data-source]').count(),6);
  assert.deepEqual(await page.locator('.ring-labels text').allTextContents(),['Julian','HG001','HG002','HG005','Watson','Venter']);
  assert.equal(await page.locator('.ring-sources').count(),0);
  assert.equal(await page.locator('.category-bubble').count(),6);
  const bubbleBoxes=await page.locator('.category-bubble').evaluateAll(items=>items.map(e=>({x:e.getBoundingClientRect().x,y:e.getBoundingClientRect().y})));
  assert.equal(bubbleBoxes[0].y,bubbleBoxes[2].y);
  assert.ok(bubbleBoxes[3].y>bubbleBoxes[0].y);
  assert.equal(bubbleBoxes[0].x,bubbleBoxes[3].x);
  const orders=await page.locator('.genome-rings g[data-source]').evaluateAll(rings=>rings.map(ring=>[...ring.children].map(e=>e.dataset.category)));
  const shared=await page.locator('.category-bubble').evaluateAll(items=>items.map(item=>item.dataset.category));
  for(const order of orders)assert.deepEqual(order,shared.filter(key=>order.includes(key)),'Category order is consistent across every ring, excluding absent categories');
  assert.equal(await page.locator('.ring-section-titles text').first().getAttribute('data-section-title'),'other');
  const sums=await page.locator('.genome-rings g[data-source]').evaluateAll(rings=>rings.map(ring=>[...ring.children].reduce((sum,e)=>sum+Number(e.dataset.arc),0)));
  assert.ok(sums.every(sum=>Math.abs(sum-100)<0.001));
  assert.equal(await page.locator('#composition-scale').inputValue(),'8');
  assert.equal(await page.locator('.ring-section-titles textPath').count(),5);
  for(const name of ['HG001','HG002','HG005'])assert.equal(await page.locator(`g[data-source="${name}"] circle[data-category="same"]`).count(),1,'Official confidence regions establish reference-match segments');
  assert.equal(await page.locator('g[data-source="Julian"] circle[data-category="same"]').count(),0,'Missing quality is not promoted to confirmed reference matches');
  const segment=page.locator('.genome-rings circle[data-category="other"]').first();
  const initial=Number(await segment.getAttribute('data-arc'));
  assert.ok(initial>0,'Nonzero categories retain nonzero arcs');
  const tickBefore=Number(await page.locator('[data-tick="1"]').getAttribute('data-position'));
  assert.ok(tickBefore>0.1 && tickBefore<0.9);
  await page.locator('#composition-scale').fill('0');
  assert.ok(Number(await segment.getAttribute('data-arc'))<initial);
  assert.equal(Number(await page.locator('[data-tick="25"]').getAttribute('data-position')),0.25);
  assert.ok(Math.abs(Number(await segment.getAttribute('data-arc'))-Number(await segment.getAttribute('data-percent')))<0.000001);
  await page.locator('#composition-scale').fill('8');
  assert.equal(Number(await page.locator('[data-tick="1"]').getAttribute('data-position')),tickBefore);
  async function checkLabelPalette(){
   const labels=await page.locator('.ring-section-titles text').evaluateAll(items=>items.map(e=>({key:e.dataset.sectionTitle,fill:e.getAttribute('fill'),backing:e.getAttribute('stroke')})));
   const luminance=color=>{const channels=[1,3,5].map(i=>parseInt(color.slice(i,i+2),16)/255).map(v=>v<=.04045?v/12.92:((v+.055)/1.055)**2.4);return channels.reduce((sum,v,i)=>sum+v*[.2126,.7152,.0722][i],0)};
   for(const label of labels){assert.equal(label.fill,await page.locator(`.genome-rings circle[data-category="${label.key}"]`).first().getAttribute('stroke'));assert.ok((luminance(label.fill)+.05)/(luminance(label.backing)+.05)>=4.5,`${label.key} text contrast`)}
  }
  await checkLabelPalette();
  const color=await segment.getAttribute('stroke');
  await page.locator('#composition-palette').click();
  assert.notEqual(await segment.getAttribute('stroke'),color);
  await checkLabelPalette();
  await segment.focus();assert.match(await page.locator('#ring-readout').innerText(),/Other \/ mixed/);
  await page.locator('.ring-details summary').click();
  assert.match(await page.locator('#metrics').innerText(),/Confirmed same\nUnavailable/);
  await page.locator('#composition-scale').fill('8');
  for(const target of ['#types','#genotypes','#depth','#consequences','#satellite-stat']) {
   for(const name of ['Julian','HG001','HG002','HG005','Watson','Venter']) assert.match(await page.locator(target).innerText(),new RegExp(name));
  }
  await page.selectOption('#chromosome','21');
  for(const name of ['Julian','HG001','HG002','HG005','Watson','Venter']) assert.ok(await page.locator(`#line circle[data-sample="${name}"]`).count()>0);
  assert.equal(await page.locator('#line polyline[data-sample="Watson"]').first().getAttribute('stroke-dasharray'),'6 4');
  assert.match(await page.locator('#satellite-stat').innerText(),/Watson\nUnavailable/);
  assert.match(await page.locator('#depth').innerText(),/Watson\nUnavailable/);
  await page.selectOption('#chromosome','MT');
  assert.equal(await page.locator('#line svg circle').count(),1,'Only the supplied mitochondrial bin is plotted');
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false,`overflow at ${width}`);
  await page.locator('#methods summary').click();assert.equal(await page.locator('#methods').getAttribute('open'),'');
  await page.locator('#present').click();assert.equal(await page.locator('body').getAttribute('class'),'presentation');
  if(await page.evaluate(()=>!!document.fullscreenElement))await page.evaluate(()=>document.exitFullscreen());
  await page.evaluate(()=>document.body.classList.remove('presentation'));
  await page.selectOption('#chromosome','1');
  await page.locator('.totals-chart').screenshot({path:join(output,`genome-rings-${width}.png`)});
  await page.screenshot({path:join(output,`genome-${width}.png`),fullPage:true});
  assert.deepEqual(errors,[]);await context.close();
 }
 console.log('Passed: simultaneous six-dataset charts, assembly styles, chromosome navigation, missing metadata, single-bin plots, methods, presentation, desktop/mobile overflow and browser errors.');
}finally{await browser.close();await new Promise(r=>server.close(r));}
