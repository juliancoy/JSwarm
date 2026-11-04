import {createServer} from 'node:http';
import {readFile,mkdir} from 'node:fs/promises';
import {createRequire} from 'node:module';
import {fileURLToPath} from 'node:url';
import {join} from 'node:path';
import assert from 'node:assert/strict';
const require=createRequire(new URL('../../package.json',import.meta.url));const {chromium,expect}=require('@playwright/test');
const view=fileURLToPath(new URL('../view/',import.meta.url));let phase='connected';
const server=createServer(async(req,res)=>{
 res.setHeader('Cache-Control','no-store');
 if(req.url.startsWith('/api/')){
  res.setHeader('Content-Type','application/json');
  if(req.url==='/api/v1/swarm/wgs'){res.end(JSON.stringify({available:true,download_status:'running',samples:['Watson','Venter'].map(sample=>({sample,files_verified:3,files_total:8,files_active:2,bytes_received:1024**3,bytes_total:2*1024**3})),storage:[{host:'storage.local',assigned_bytes:2*1024**3,free_bytes_at_start:100*1024**3}],index:{status:'running',characters_processed:50,characters_total:100},qc:{reads_processed:100000},alignment:{completed:3,status:'mapping'}}));}
  else if(req.url==='/api/v1/swarm/connections'){
   const now=Date.now()/1000;const hosts=['desktop.local','laptop.local',...(phase==='joined'?['new-machine.local']:[])].map(host=>({host,ready:true,accepting_jobs:host!=='laptop.local',status:phase==='disconnected'&&host==='laptop.local'?'disconnected':'connected',checked_at:now,last_seen:now}));
   res.end(JSON.stringify({hosts,inventory_available:true,sampled_at:now,stale_after_seconds:5}));
  }else res.end(JSON.stringify({network:{devices:[]},metrics:{hosts:[],history:[]},run:{hosts:[]}}));return;
 }
 const name=req.url==='/'?'task-manager.html':req.url.slice(1);
 if(!/^[a-z0-9.-]+$/.test(name)){res.writeHead(404).end();return}
 try{const content=await readFile(join(view,name));res.setHeader('Content-Type',name.endsWith('.js')?'application/javascript':name.endsWith('.css')?'text/css':'text/html');res.end(content)}catch{res.writeHead(404).end()}
});await new Promise(r=>server.listen(0,'127.0.0.1',r));const browser=await chromium.launch({executablePath:'/usr/bin/google-chrome',headless:true});
try{await mkdir('/tmp/swarm-connection-review',{recursive:true});for(const width of [390,1440]){
 phase='connected';const page=await browser.newPage({viewport:{width,height:1000}});const errors=[];page.on('pageerror',e=>errors.push(e.message));await page.goto(`http://127.0.0.1:${server.address().port}/`);
 await expect(page.locator('.machine-connections')).toContainText('2 / 2 machines connected');
 await expect(page.locator('.wgs-progress')).toContainText('Watson');await expect(page.locator('.wgs-progress')).toContainText('Venter');await expect(page.locator('.wgs-progress')).toContainText('100,000 reads processed');
 await expect(page.locator('.machine-connections .connection-card').filter({hasText:'laptop.local'})).toContainText('Connected · worker jobs paused');
 phase='disconnected';await expect(page.locator('.machine-connections .connection-card').filter({hasText:'laptop.local'})).toHaveAttribute('data-state','disconnected',{timeout:3500});
 phase='joined';await expect(page.locator('.machine-connections .connection-card')).toHaveCount(3,{timeout:3500});await expect(page.locator('.machine-connections')).toContainText('3 / 3 machines connected');
 await page.evaluate(()=>window.dispatchEvent(new Event('offline')));await expect(page.locator('.machine-connections')).toContainText('Browser offline');assert.equal(await page.locator('.machine-connections .connection-card[data-state=connected]').count(),0);
 await page.evaluate(()=>window.dispatchEvent(new Event('online')));await expect(page.locator('.machine-connections')).toContainText('3 / 3 machines connected');
 assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);assert.deepEqual(errors,[]);await page.screenshot({path:`/tmp/swarm-connection-review/task-manager-${width}.png`,fullPage:true});await page.close();
}console.log('Passed: authenticated heartbeat states, join, disconnect, reconnect, paused worker, browser network recovery, desktop/mobile overflow and browser errors.');}finally{await browser.close();await new Promise(r=>server.close(r))}
