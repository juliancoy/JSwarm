import assert from 'node:assert/strict'
import {createServer} from 'node:http'
import {mkdir,readFile,writeFile} from 'node:fs/promises'
import {join,resolve} from 'node:path'
import {fileURLToPath} from 'node:url'
import {chromium,expect} from '@playwright/test'
const view=fileURLToPath(new URL('../view/',import.meta.url)),output=resolve(process.argv[2]||'artifacts/history-ui')
await mkdir(output,{recursive:true})
const rows=[
 {id:'genome',kind:'genome',title:'Genome analysis — baseline',status:'running',run_id:'genome-run',started_at:'2026-10-26T14:00:00Z',progress:{total:214,completed:108,failed:0},machines:[{name:'minipc.local',status:'running',completed_tasks:108,executed_tasks:108,cpu_seconds:600,cpu_measured_tasks:108,estimated_cpu_slot_seconds:700,cpu_estimated_tasks:108,tasks:['DNA batch']}]},
 {id:'web',kind:'tests',title:'Web tests — Web layout tests',status:'passed',run_id:'web-run',started_at:'2026-10-26T13:00:00Z',finished_at:'2026-10-26T13:05:00Z',progress:{total:12,completed:12,failed:0},machines:[{name:'compai.local',status:'passed',completed_tasks:12,executed_tasks:12,cpu_seconds:null,cpu_measured_tasks:0,estimated_cpu_slot_seconds:900,cpu_estimated_tasks:12,tasks:['Portal frontend · events.test.ts']}]},
 {id:'queued',kind:'tests',title:'Distributed tests — Full suite',status:'queued',submitted_at:'2026-10-26T15:00:00Z',progress:{total:null},machines:[]},
]
let unauthorized=false,unavailable=false
const server=createServer(async(req,res)=>{
 const path=new URL(req.url,'http://localhost').pathname
 if(req.method!=='GET'){res.writeHead(405).end();return}
 if(path==='/api/v1/swarm/history'){res.setHeader('Content-Type','application/json');res.statusCode=unauthorized?401:unavailable?503:200;res.end(JSON.stringify({jobs:rows}));return}
 const name={'/history':'history.html','/history.js':'history.js','/history-contributions.js':'history-contributions.js','/history.css':'history.css','/style.css':'style.css'}[path]
 if(!name){res.writeHead(404).end();return}
 res.setHeader('Content-Type',name.endsWith('.js')?'text/javascript':name.endsWith('.css')?'text/css':'text/html');res.end(await readFile(join(view,name)))
})
await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));const base=`http://127.0.0.1:${server.address().port}`
const browser=await chromium.launch({headless:true,...(process.env.CHROME_PATH?{executablePath:process.env.CHROME_PATH}:{})}),results=[]
try{
 for(const width of [390,1440]){
  const context=await browser.newContext({viewport:{width,height:1000}}),page=await context.newPage(),errors=[]
  page.on('pageerror',e=>errors.push(e.message))
  await page.goto(base+'/history')
  await expect(page.locator('.history-card')).toHaveCount(3)
  await expect(page.locator('.history-card').first().getByRole('heading',{level:2})).toHaveText('Genome analysis — baseline')
  await expect(page.getByText('108 / 214 batches complete · 0 failed')).toBeVisible()
  const contributions=page.getByRole('region',{name:'Computer contributions'});
  await expect(contributions.locator('tbody tr').first()).toContainText('minipc.local');
  await page.getByLabel('Rank computers by').selectOption('estimated_cpu_slot_seconds');
  await expect(contributions.locator('tbody tr').first()).toContainText('compai.local');
  await page.getByLabel('Rank computers by').selectOption('cpu_seconds');
  await expect(contributions.locator('tbody tr').first()).toContainText('minipc.local');
  await expect(contributions).toContainText('Not recorded');
  await page.getByLabel('Workload',{exact:true}).selectOption('tests');await expect(page.locator('.history-card')).toHaveCount(2)
  await page.getByLabel('Status',{exact:true}).selectOption('finished');await expect(page.locator('.history-card')).toHaveCount(1)
  await page.getByText('Machines and tasks (1)').click();await expect(page.getByText('Portal frontend · events.test.ts')).toBeVisible()
  await page.getByLabel('Find a run').fill('missing');await expect(page.getByText('No runs match these filters.')).toBeVisible()
  await page.getByLabel('Find a run').fill('');await page.getByLabel('Workload',{exact:true}).selectOption('all');await page.getByLabel('Status',{exact:true}).selectOption('all')
  unavailable=true;await page.getByRole('button',{name:'Refresh',exact:true}).click();await expect(page.getByRole('status')).toContainText('last loaded entries');await expect(page.locator('.history-card')).toHaveCount(3);unavailable=false
  await page.getByRole('button',{name:'Refresh',exact:true}).click();await expect(page.getByRole('status')).toContainText('recent runs')
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false)
  await page.screenshot({path:join(output,`history-${width}.png`),fullPage:true})
  unauthorized=true;await page.getByRole('button',{name:'Refresh',exact:true}).click();await expect(page.locator('.history-card')).toHaveCount(0);await expect(page.locator('#contributions')).toBeEmpty();await expect(page.getByRole('link',{name:'Sign in to Swarm Control'})).toBeVisible();unauthorized=false
  assert.deepEqual(errors,[]);results.push({width,status:'passed',checks:['genome-and-web-titles','progress','filters','machine-details','search','failed-refresh-keeps-history','session-expiry-clears-history','no-overflow']});await context.close()
 }
}finally{await browser.close();server.closeAllConnections();await new Promise(resolve=>server.close(resolve));await writeFile(join(output,'history-review.json'),JSON.stringify(results,null,2))}
console.log(JSON.stringify(results))
