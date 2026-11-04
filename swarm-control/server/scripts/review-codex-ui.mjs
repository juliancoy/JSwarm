import {execFileSync} from 'node:child_process'
import {createServer} from 'node:http'
import {readFile,mkdir,writeFile} from 'node:fs/promises'
import {fileURLToPath} from 'node:url'
import {resolve,join} from 'node:path'
import {createRequire} from 'node:module'
import assert from 'node:assert/strict'
const web=fileURLToPath(new URL('../../',import.meta.url))
const {chromium}=createRequire(join(web,'package.json'))('@playwright/test')
const apiSource=await readFile(fileURLToPath(new URL('../api.py',import.meta.url)),'utf8')
const contentSecurityPolicy=apiSource.match(/'Content-Security-Policy':"([^"]+)"/)[1]
const view=fileURLToPath(new URL('../view/',import.meta.url))
export async function reviewCodexUi(output){
 await mkdir(output,{recursive:true})
 execFileSync(process.execPath,[fileURLToPath(new URL('./review-resource-network.mjs',import.meta.url)),output],{stdio:'inherit',env:process.env})
 if(!process.env.TASK_MANAGER_ONLY)execFileSync(process.execPath,[fileURLToPath(new URL('./review-history-ui.mjs',import.meta.url)),output],{stdio:'inherit',env:process.env})
 const commands=[],results=[],researchRequests=[];let researchJobs=[];let failNext=false;let ownerConnected=true,statusUnauthorized=false
 const threads=[{id:'design',name:'Improve the remote control workspace',cwd:'/workspace/Organization',updated_at:1792880000,activity:'running'},{id:'release',name:'Review the release checks',cwd:'/workspace/OrgPortal',updated_at:1792876000,activity:'idle'}]
 threads.push(...Array.from({length:18},(_,index)=>({id:'history-'+index,name:'Earlier workspace task '+(index+1),cwd:'/workspace/Organization',updated_at:1792800000-index*300,activity:'idle'})))
 for(const thread of threads){thread.display_title=thread.name;thread.name='Raw database name';thread.title='Initial prompt text'}
 const lanFixture={updated:'2026-10-24T20:00:00Z',devices:['worker','queued','stopped','failed','passed'].map((name,index)=>({hostname:name+'.local',address:'10.0.0.'+(index+2),reachable:true})),network:{interface:'eth0',subnet:'10.0.0.0/24',gateway:'10.0.0.1',interfaces:[{name:'eth0',rx_mbps:1.25,tx_mbps:0.5,capacity_mbps:1000}]},run:{id:'fixture-run',status:'running',hosts:['running','queued','stopped','failed','passed'].map((status,index)=>({host:['worker','queued','stopped','failed','passed'][index]+'.local',status,jobs:[{name:'Fixture tests',status}]}))}};
 const server=createServer(async(req,res)=>{
  try{
   res.setHeader('Content-Security-Policy',contentSecurityPolicy)
   const url=new URL(req.url,'http://localhost')
   if(url.pathname==='/api/v1/swarm/research/jobs'){
    res.setHeader('Content-Type','application/json');if(statusUnauthorized){res.statusCode=401;res.end('{}');return}
    if(req.method==='POST'){let body='';for await(const chunk of req)body+=chunk;const value=JSON.parse(body);researchRequests.push(value);
      researchJobs=value.cancel?researchJobs.map(j=>({...j,status:'stopped'})):[{id:value.request_id,kind:'research',status:'queued',submitted_at:'2026-10-25T19:00:00Z'}];res.end(JSON.stringify({accepted:true}));return}
    res.end(JSON.stringify({profiles:['baseline'],jobs:researchJobs}));return
   }
   if(['/api/v1/swarm/connections','/api/v1/swarm/wgs'].includes(url.pathname)){res.setHeader('Content-Type','application/json');res.statusCode=statusUnauthorized?401:200;res.end(JSON.stringify(url.pathname.endsWith('/wgs')?{available:false}:{hosts:[]}));return}
   if(url.pathname==='/api/v1/swarm/task-manager'){
    res.setHeader('Content-Type','application/json');res.statusCode=statusUnauthorized?401:200;
    res.end(JSON.stringify({network:{devices:[{hostname:'worker.local',ip:'10.0.0.2',mac:'aa'}]},metrics:{history:[{sampled_at:Date.now()/1000-240,hosts:[{host:'worker.local',status:'live',sampled_at:Date.now()/1000-240,percent:20,cpus:8}]},{sampled_at:Date.now()/1000-235,hosts:[{host:'worker.local',status:'live',sampled_at:Date.now()/1000-235,percent:25,cpus:8}]}],total_percent:42,live_hosts:1,total_hosts:1,measured_logical_cpus:8,activity_status:'available',test_hosts:1,measured_test_hosts:1,active_test_percent:42,hosts:[{host:'worker.local',status:'live',sampled_at:Date.now()/1000,window_seconds:5,sample_age_seconds:1,percent:42,ram_percent:35,disk_busy_percent:82,disks:[{name:'sda',identity:'fixture-disk',model:'Fixture SSD',status:'live',sampled_at:Date.now()/1000,read_bytes_per_second:12000000,write_bytes_per_second:8000000,read_percent:12,write_percent:8,capacity_bytes_per_second:100000000,capacity_basis:'observed sustained'}],network_mbps:7,network_interfaces:[{name:'eth0',ifindex:2,state:'up',status:'live',sampled_at:Date.now()/1000,network_mbps:7,rx_mbps:4,tx_mbps:3}],cpus:8,workload_phase:'testing',hardware:{gpus:[{pci_id:'gpu-1',name:'Fixture GPU',utilization_percent:25,memory_total_mib:100,memory_free_mib:60}]}}]},run:{id:'fixture',status:'running',hosts:[{host:'worker.local',status:'running',jobs:[{name:'Browser checks',status:'running'}]}]}}));return
   }
   if(url.pathname==='/task-manager'){res.setHeader('Content-Type','text/html');res.end(await readFile(join(view,'task-manager.html')));return}
   if(url.pathname==='/api/v1/swarm/research'){res.setHeader('Content-Type','application/json');res.statusCode=statusUnauthorized?401:200;res.end(JSON.stringify({observed_at:'2026-10-25T19:00:00Z',run:{status:'passed',completed:214,total:214,failed:0,workers:6,verified:true,updated_at:'2026-10-25T18:30:00Z'},public_downloads:[{name:'Watson',status:'partial download; completion unconfirmed'}]}));return}
   if(url.pathname==='/api/v1/swarm/status'){res.setHeader('Content-Type','application/json');res.statusCode=statusUnauthorized?401:200;res.end(JSON.stringify(lanFixture));return}
   if(url.pathname==='/research.html'){res.setHeader('Content-Type','text/html');res.end(await readFile(join(view,'research.html')));return}
   if(url.pathname==='/swarm-status'){res.setHeader('Content-Type','text/html');res.end(await readFile(join(view,'swarm-status.html')));return}
   if(url.pathname==='/api/v1/master/codex/queue'&&req.method==='GET'){res.setHeader('Content-Type','application/json');res.end(JSON.stringify({messages:[]}));return}
   if(url.pathname.startsWith('/api/')){
    res.setHeader('Content-Type','application/json')
    if(req.method==='POST'){let body='';for await(const chunk of req)body+=chunk;commands.push({...JSON.parse(body),...(url.pathname.endsWith('/queue')?{action:'send'}:{})});await new Promise(resolve=>setTimeout(resolve,300));if(failNext){failNext=false;res.writeHead(503);res.end(JSON.stringify({error:{message:'Synthetic uncertain delivery'}}));return}res.end(JSON.stringify({accepted:true,message:'Instruction accepted by the synthetic thread.'}));return}
    const item=threads.find(t=>url.pathname.endsWith('/'+t.id))
    res.end(JSON.stringify(item?{...item,model:'Codex',owner_connected:ownerConnected,messages:[{role:'user',text:'Make the remote workspace easier to scan and use on a phone.'},{role:'assistant',text:'I’ll organize the conversation list, clarify connection status and keep the instruction composer stable while updates arrive.\n\nThe active conversation will stay visible, and your draft will be preserved.'}]}:{threads,next_offset:null,machine:'Fixture workstation',connected:true}));return
   }
   if(url.pathname==='/'){res.setHeader('Content-Type','text/html');res.end('<!doctype html><html lang="en"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Codex UI review</title><link rel="stylesheet" href="/style.css"><link rel="stylesheet" href="/codex.css"><body><main><section id="codex-context"></section></main><script type="module" src="/codex.js"></script></body></html>');return}
   if(!['/monitor.js','/monitor-model.js','/monitor.css','/swarm-control.svg','/marked-vendor.js','/message-markdown.js','/wgs-progress.js','/machine-connections.js','/task-manager.js','/task-manager.css','/tests.css','/levels.js','/levels-model.js','/resource-history.js','/cpu-report.js','/snapshot-fetch.js','/docker-downloads.js','/research.css','/research.js','/style.css','/codex.css','/codex.js','/codex-screenshots.js','/codex-voice.js','/codex-queue.js','/swarm-status.css','/swarm-status.js','/swarm-machines.js'].includes(url.pathname)){res.writeHead(404).end();return}
   res.setHeader('Content-Type',url.pathname.endsWith('.js')?'text/javascript':'text/css');res.end(await readFile(join(view,url.pathname.slice(1))))
  }catch(error){res.writeHead(500).end(error.message)}
 })
 await new Promise(r=>server.listen(0,'127.0.0.1',r));const base=`http://127.0.0.1:${server.address().port}`
 const browser=await chromium.launch({headless:true,args:['--disable-gpu'],...(process.env.CHROME_PATH?{executablePath:process.env.CHROME_PATH}:{})})
 try{for(const width of [390,1440]){
  const context=await browser.newContext({viewport:{width,height:1000},reducedMotion:'reduce'}),page=await context.newPage(),errors=[]
  page.on('pageerror',e=>errors.push(e.message))
  const resourceRequests=[];page.on('request',r=>{if(r.url().includes('/api/'))resourceRequests.push(r.url())});
  await page.goto(base+'/task-manager');
  await page.getByRole('heading',{name:'Swarm Task Manager',exact:true}).waitFor();
  await page.waitForFunction(()=>document.querySelector('#levels-status')?.textContent.startsWith('Updated'));
  await page.waitForFunction(()=>document.querySelector('#task-summary')?.textContent.includes('Current run: running'));
  assert.match(await page.locator('#task-summary').textContent(),/Current run: running/);
  await page.getByLabel('Swarm resource usage').getByRole('button',{name:'CPU',exact:true}).click();
  assert.match(await page.locator('#levels-plot path[fill="none"]').first().getAttribute('d'),/L/);
  await page.reload();
  await page.waitForFunction(()=>document.querySelector('#levels-status')?.textContent.startsWith('Updated'));
  await page.getByLabel('Swarm resource usage').getByRole('button',{name:'CPU',exact:true}).click();
  assert.match(await page.locator('#levels-plot path[fill="none"]').first().getAttribute('d'),/L/);
  await page.locator('#task-hosts summary').click();
  assert.match(await page.locator('#task-hosts').textContent(),/Browser checks · running/);
  for(const [name,value] of [['CPU','42.0%'],['GPU','25.0%'],['GPU memory','40.0%'],['Memory','35.0%'],['Disk','12.0%'],['Network','3.0 Mb/s']]){
   await page.getByLabel('Swarm resource usage').getByRole('button',{name,exact:true}).click();
   assert.match(await page.locator('#levels-legend').textContent(),new RegExp(value.replace(/[.*+?^${}()|[\]\\]/g,'\\$&')));
   if(name==='Network')assert.match(await page.locator('#levels-legend').textContent(),/Receive4\.0 Mb\/s/);
   assert.equal(await page.locator('#levels-plot svg').count(),name==='Network'?2:1);
   assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
  }
  await page.screenshot({path:join(output,`task-manager-${width}.png`),fullPage:true});
  assert(resourceRequests.every(url=>new URL(url).origin===base&&['/api/v1/swarm/task-manager','/api/v1/swarm/connections','/api/v1/swarm/wgs'].includes(new URL(url).pathname)),'Resource view must use the authenticated same-origin feed');
  statusUnauthorized=true;
  await page.waitForFunction(()=>document.querySelector('#levels-status')?.textContent.includes('Sign in to Swarm Control'),{},{timeout:10000});
  assert.equal(await page.locator('#levels-legend').textContent(),'');
  assert.equal(await page.locator('#levels-cpu-summary').textContent(),'');
  assert.equal(await page.locator('#task-hosts').textContent(),'');
  statusUnauthorized=false;
  await page.waitForFunction(()=>document.querySelector('#levels-status')?.textContent.startsWith('Updated'),{},{timeout:10000});
  assert.match(await page.locator('#levels-legend').textContent(),/3.0 Mb/);
  if(process.env.TASK_MANAGER_ONLY){assert.deepEqual(errors,[]);results.push({width,status:'passed',checks:['history-restored-after-reload','six-resources','session-expiry','same-origin-feed','no-overflow']});await context.close();continue;}
  resourceRequests.length=0;
  await page.goto(base+'/research.html');await page.getByText('214 / 214',{exact:true}).waitFor()
  assert.equal(await page.locator('.pipeline button').count(),6)
  await page.getByRole('button',{name:'Queue analysis',exact:true}).click()
  await page.getByText('Analysis queued.',{exact:false}).waitFor()
  assert.equal(researchRequests.at(-1).profile,'baseline')
  await page.locator('#job-list').getByRole('button',{name:'Cancel',exact:true}).click()
  await page.locator('#job-list').getByText(/stopped/).waitFor()
  assert.ok(researchRequests.at(-1).cancel)

  await page.getByRole('button',{name:/02.*Alignments/}).click()
  assert.match(await page.locator('#detail').textContent(),/whole-genome coverage/)
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false)
  await page.screenshot({path:join(output,`research-${width}.png`),fullPage:true})
  statusUnauthorized=true;await page.getByRole('button',{name:'Refresh status',exact:true}).click()
  await page.getByText(/Sign in to Swarm Control to read private execution progress/).waitFor()
  assert.equal(await page.locator('#metrics').isVisible(),false);statusUnauthorized=false
  await page.goto(base);await page.evaluate(()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve))));
  const mapLink=page.getByRole('link',{name:'Swarm status ↗',exact:true});assert.equal(await mapLink.getAttribute('href'),'/swarm-status');assert.equal(await mapLink.getAttribute('target'),'_blank');assert.equal(await mapLink.isVisible(),true)
  await page.screenshot({path:join(output,`codex-locked-${width}.png`),fullPage:false})
  await page.goto(base+'/swarm-status');await page.getByRole('heading',{name:'LAN status',exact:true}).waitFor()
  await page.waitForFunction(()=>document.querySelectorAll('.machine-card').length===5)
  for(const state of ['running','queued','stopped','failed','passed'])assert.equal(await page.locator('.machine-card[data-state='+state+']').count(),1)
  assert(await page.locator('#devices').textContent().then(text=>text.includes('worker.local')))
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false)
  await page.screenshot({path:join(output,`codex-lan-${width}.png`),fullPage:false})
  await page.locator('.machine-card').first().focus();await page.keyboard.press('Enter');await page.getByRole('heading',{name:'worker.local',exact:true,level:2}).waitFor()
  assert.equal(await page.locator('.machine-task[data-state=running]').count(),1)
  await page.screenshot({path:join(output,`codex-machine-${width}.png`),fullPage:false})
  await page.getByRole('button',{name:'Refresh',exact:true}).click();assert(page.url().includes('#machine='))
  await page.getByRole('link',{name:'← All machines',exact:true}).click();await page.locator('.machine-card').first().waitFor();assert.equal(await page.locator('.machine-card:visible').count(),5)
  await page.locator('.machine-card').first().click();await page.getByRole('heading',{name:'worker.local',level:2,exact:true}).waitFor();await page.goBack();await page.locator('.machine-card').first().waitFor()

  statusUnauthorized=true;await page.getByRole('button',{name:'Refresh',exact:true}).click()
  await page.waitForFunction(()=>document.getElementById('feedback')?.textContent.includes('Pair this browser'))
  assert.equal(await page.locator('#devices').textContent(),'');statusUnauthorized=false;await page.goto(base)

  await page.locator('.codex-unlock input').fill('synthetic-only');await page.getByRole('button',{name:'Connect',exact:true}).click()
  await page.locator('.codex-thread').first().waitFor()
  assert(await page.locator('.codex-thread').evaluateAll(nodes=>nodes.every(node=>[...node.children].every(child=>child.getBoundingClientRect().bottom<=node.getBoundingClientRect().bottom))), 'Conversation rows must contain their text without overlap')
  const workspace=await page.locator('.codex-workspace').boundingBox(),pagination=await page.locator('.codex-pagination').boundingBox();assert(pagination.y+pagination.height<=workspace.y+workspace.height+1,'Pagination must remain inside the workspace with twenty threads')
  await page.screenshot({path:join(output,`codex-list-${width}.png`),fullPage:false})
  await page.getByRole('searchbox').fill('missing thread');assert.equal(await page.locator('.codex-no-results').isVisible(),true)
  await page.getByRole('searchbox').fill('release');assert.equal(await page.locator('.codex-thread:visible').count(),1)
  await page.getByRole('searchbox').fill('');await page.locator('.codex-thread').first().click()
  await page.locator('.codex-message').first().waitFor()
  assert.match(await page.locator('.codex-activity').textContent(),/Codex is working/)
  assert.equal(await page.getByRole('button',{name:'Send instruction',exact:true}).isDisabled(),true)
  assert.equal(await page.locator('.codex-thread-info h2').textContent(),'Improve the remote control workspace')
  assert.equal(await page.locator('.codex-thread').first().locator('strong').textContent(),'Improve the remote control workspace')
  await page.locator('.codex-compose textarea').fill('Keep this draft while refreshing.')
  await page.getByLabel('Instruction delivery').selectOption('send')
  await page.locator('.codex-message').first().evaluate(node=>node.dataset.retained='yes')
  if(width<800){await page.getByRole('button',{name:'← Conversations',exact:true}).click()}
  await Promise.all([page.waitForResponse(r=>r.url().endsWith('/threads/design')),page.getByRole('button',{name:'Refresh',exact:true}).click()]);await page.waitForTimeout(100)
  await page.waitForFunction(()=>document.querySelector('.codex-message')?.dataset.retained==='yes')
  assert.equal(await page.getByLabel('Instruction delivery').inputValue(),'send')
  assert.equal(await page.locator('textarea').inputValue(),'Keep this draft while refreshing.')
  if(width<800)await page.locator('.codex-thread').first().click()
  assert.match(await page.locator('.codex-message>div').last().evaluate(node=>getComputedStyle(node).fontFamily),/system-ui/)
  await page.locator('.codex-activity:visible').waitFor()
  const imageData=await page.evaluate(()=>{const canvas=document.createElement('canvas');canvas.width=160;canvas.height=90;const ctx=canvas.getContext('2d');ctx.fillStyle='#155e59';ctx.fillRect(20,20,100,50);return canvas.toDataURL().split(',')[1]})
  const screenshot={name:'screen.png',mimeType:'image/png',buffer:Buffer.from(imageData,'base64')}
  await page.getByLabel('Choose screenshots').setInputFiles(screenshot);await page.locator('.codex-screenshot-previews img').waitFor()
  assert.equal(await page.locator('.codex-screenshot-previews img').count(),1)
  await page.locator('textarea').evaluate((node,data)=>{const bytes=Uint8Array.from(atob(data),c=>c.charCodeAt(0)),transfer=new DataTransfer();transfer.items.add(new File([bytes],'pasted.png',{type:'image/png'}));node.dispatchEvent(new ClipboardEvent('paste',{clipboardData:transfer,bubbles:true,cancelable:true}))},imageData)
  await page.waitForFunction(()=>document.querySelectorAll('.codex-screenshot-previews img').length===2)
  await page.getByRole('button',{name:'Remove pasted.png',exact:true}).click();assert.equal(await page.locator('.codex-screenshot-previews img').count(),1)
  await page.locator('.codex-compose').evaluate((node,data)=>{const bytes=Uint8Array.from(atob(data),c=>c.charCodeAt(0)),transfer=new DataTransfer();transfer.items.add(new File([bytes],'dropped.png',{type:'image/png'}));node.dispatchEvent(new DragEvent('drop',{dataTransfer:transfer,bubbles:true,cancelable:true}))},imageData)
  await page.waitForFunction(()=>document.querySelectorAll('.codex-screenshot-previews img').length===2)
  const transparent=await page.locator('.codex-screenshot-previews img').first().evaluate(async img=>{await img.decode();const canvas=document.createElement('canvas');canvas.width=img.naturalWidth;canvas.height=img.naturalHeight;const ctx=canvas.getContext('2d');ctx.drawImage(img,0,0);return ctx.getImageData(0,0,1,1).data[3]===0});assert.equal(transparent,true)
  await page.screenshot({path:join(output,`codex-conversation-${width}.png`),fullPage:true})
  const before=commands.length;await page.locator('textarea').press('Control+Enter')
  await page.getByRole('button',{name:'Sending…',exact:true}).waitFor()
  assert.equal(await page.locator('textarea').getAttribute('readonly'),'')
  assert.match(await page.locator('.codex-activity').textContent(),/Sending your instruction/)
  await page.waitForFunction(()=>document.querySelector('textarea')?.value==='')
  assert.equal(commands.length,before+1);assert.equal(commands.at(-1).thread_id,'design');assert.equal(commands.at(-1).action,'send')
  assert.equal(commands.at(-1).images.length,2);assert.match(commands.at(-1).images[0].data_url,/^data:image\/(png|webp);base64,/);assert.equal(await page.locator('.codex-screenshot-previews img').count(),0)
  await page.getByLabel('Choose screenshots').setInputFiles(screenshot);await page.locator('.codex-screenshot-previews img').waitFor();failNext=true
  await page.getByRole('button',{name:'Send instruction',exact:true}).click();await page.waitForFunction(()=>document.querySelector('.codex-status')?.textContent.includes('Synthetic uncertain delivery'));assert.equal(await page.locator('.codex-screenshot-previews img').count(),1)
  const uncertainId=commands.at(-1).request_id;await page.getByLabel('Choose screenshots').setInputFiles({name:'notes.txt',mimeType:'text/plain',buffer:Buffer.from('not an image')});await page.waitForFunction(()=>document.querySelector('.codex-screenshots [role=status]')?.textContent.includes('Choose a PNG'));assert.equal(await page.locator('.codex-screenshot-previews img').count(),1);await page.getByRole('button',{name:'Send instruction',exact:true}).click();await page.waitForFunction(()=>!document.querySelector('.codex-screenshot-previews img'));assert.equal(commands.at(-1).request_id,uncertainId);assert.equal(commands.at(-1).text,'')
  threads[0].activity='idle';
  await page.waitForFunction(()=>document.querySelector('.codex-activity')?.dataset.state==='idle',{},{timeout:8000})
  assert.match(await page.locator('.codex-activity').textContent(),/ready for your next instruction/)
  threads[0].activity='unknown';
  await page.waitForFunction(()=>document.querySelector('.codex-activity')?.dataset.state==='unknown',{},{timeout:8000})
  assert.match(await page.locator('.codex-activity').textContent(),/status unavailable/)
  threads[0].activity='running';
  ownerConnected=false;
  await page.waitForFunction(()=>document.querySelector('.codex-thread-info')?.dataset.connected==='false',{},{timeout:8000})
  await page.locator('textarea').fill('Queue while disconnected');assert.equal(await page.getByRole('button',{name:'Send instruction',exact:true}).isDisabled(),false);ownerConnected=true
  await page.getByRole('button',{name:'Lock',exact:true}).click();assert.equal(await page.locator('.codex-workspace').isVisible(),false)
  assert.equal(await page.locator('textarea').inputValue(),'')
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false)
  assert.deepEqual(errors,[]);results.push({width,status:'passed',checks:['screenshot-picker','screenshot-paste','screenshot-drop','screenshot-removal','screenshot-alpha','screenshot-only-send','failed-send-keeps-images','uncertain-retry-keeps-request-id','remote-task-manager-six-resources','same-origin-resource-feed','expired-session-clears-resources','resource-reconnect','running-idle-unknown-feedback','sending-feedback','keyboard-submit','blank-send-disabled','connect','empty-state','search','select-thread','stable-refresh','draft-retained','delivery-retained','send-once','lock','no-overflow','no-script-errors','empty-search','disconnected-thread','long-list-pagination','lan-machine-colors','machine-detail','machine-back','machine-refresh']});await context.close()
 }}finally{await browser.close();server.closeAllConnections();await new Promise(r=>server.close(r));await writeFile(join(output,'codex-review.json'),JSON.stringify({mode:'synthetic-local-only',results},null,2)+'\n')}
 return results
}
if(process.argv[1]&&resolve(process.argv[1])===fileURLToPath(import.meta.url))await reviewCodexUi(resolve(process.argv[2]||'artifacts/codex-ui'))
