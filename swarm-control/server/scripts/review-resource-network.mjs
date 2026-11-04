import assert from 'node:assert/strict'
import {createServer} from 'node:http'
import {readFile,mkdir,writeFile} from 'node:fs/promises'
import {fileURLToPath} from 'node:url'
import {join} from 'node:path'
import {chromium,expect} from '@playwright/test'
const view=fileURLToPath(new URL('../view/',import.meta.url)),output=process.argv[2]||'/tmp/resource-network-review';await mkdir(output,{recursive:true})
const model=JSON.parse(await readFile(new URL('./network-human-model.json',import.meta.url),'utf8')),review={model,checks:[],humanReview:'pending'}
const network=time=>({devices:[{hostname:'worker.local',ip:'10.42.0.2',mac:'aa'},{ip:'10.42.0.3',mac:'bb'}],link_traffic:{updated:new Date(time).toISOString(),peers:{aa:{rx_mbps:10,tx_mbps:4}}}})
const server=createServer(async(req,res)=>{
 const url=new URL(req.url,'http://localhost');res.setHeader('Cache-Control','no-store')
 if(req.method!=='GET'){res.writeHead(405).end();return}
 if(url.pathname==='/api/v1/swarm/task-manager'){
  const now=Date.now(),host={host:'worker.local',addresses:['10.42.0.2'],sampled_at:now/1000,status:'live',percent:20,cpus:4,disks:[{name:'sda',model:'Fixture disk with a long model name',identity:'disk-one',status:'live',sampled_at:now/1000,read_bytes_per_second:60000000,write_bytes_per_second:40000000,read_percent:60,write_percent:40,capacity_bytes_per_second:100000000,capacity_basis:'observed sustained'},{name:'sdb',identity:'disk-two',status:'live',sampled_at:now/1000,read_bytes_per_second:1000000,write_bytes_per_second:0,read_percent:null,write_percent:null}],network_interfaces:[{name:'eth0',ifindex:2,link_speed_mbps:100,link_duplex:'full',state:'up',status:'live',sampled_at:now/1000,rx_mbps:4,tx_mbps:10,network_mbps:14},{name:'wlan0',ifindex:3,link_rx_mbps:72.2,link_tx_mbps:65,state:'up',status:'live',sampled_at:now/1000,rx_mbps:2,tx_mbps:1,network_mbps:3}]};
  const hosts=[host,...['compai.local','codecollective.local','julian-latitude.local','julian-minipc.local','wolf-latitude.local'].map((name,n)=>({...host,host:name,addresses:['10.42.0.'+(n+10)],network_interfaces:host.network_interfaces.map(i=>({...i,tx_mbps:55.3+n,rx_mbps:50.3+n,network_mbps:105.6+2*n}))}))];
  hosts[1].disks=host.disks.map(disk=>disk.name==='sda'?{...disk,read_bytes_per_second:6000000000,write_bytes_per_second:4000000000,capacity_bytes_per_second:10000000000}:disk);
  host.network_interfaces.push({name:'enp9s0',ifindex:9,state:'down',status:'disconnected',sampled_at:now/1000,network_mbps:null,rx_mbps:null,tx_mbps:null});
  hosts.push({host:'no-interface.local',status:'live',sampled_at:now/1000,network_interfaces:[]});
  const history=Array.from({length:13},(_,n)=>{const time=now-60000+n*5000;return {sampled_at:time/1000,hosts:hosts.map(host=>({...host,sampled_at:time/1000,disks:(host.disks||[]).map(disk=>({...disk,sampled_at:time/1000})),network_interfaces:host.network_interfaces.map(iface=>({...iface,sampled_at:time/1000}))})),network:network(time)}});
  res.setHeader('Content-Type','application/json');res.end(JSON.stringify({network:network(now),metrics:{hosts,history},run:{hosts:[]}}));return
 }
 if(url.pathname==='/api/v1/swarm/connections'){res.setHeader('Content-Type','application/json');res.end('{"hosts":[]}');return}
 const name=url.pathname==='/task-manager'?'task-manager.html':url.pathname.slice(1)
 if(!/^[a-z-]+\.(?:js|css|html|svg)$/.test(name)){res.writeHead(404).end();return}
 try{res.setHeader('Content-Type',name.endsWith('.js')?'text/javascript':name.endsWith('.css')?'text/css':'text/html');res.end(await readFile(join(view,name)))}catch{res.writeHead(404).end()}
})
await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve))
const browser=await chromium.launch({headless:true,...(process.env.CHROME_PATH?{executablePath:process.env.CHROME_PATH}:{})})
try{
 const page=await browser.newPage({viewport:{width:1440,height:1000}}),errors=[];page.on('pageerror',error=>errors.push(error.message))
 await page.clock.install()
 await page.goto(`http://127.0.0.1:${server.address().port}/task-manager`)
 await expect(page.locator('#levels-status')).toContainText('Updated')
 await page.getByLabel('Swarm resource usage').getByRole('button',{name:'Network',exact:true}).click()
 await page.getByLabel('Separate send / receive').check()
 await page.clock.pauseAt(new Date(Date.now()+1000))
 const paths=await page.locator('#levels-plot path[fill="none"]').evaluateAll(paths=>paths.map(path=>path.getAttribute('d')))
 assert.equal(paths.length,24,'Six machines with two interfaces should each have send and receive series')
 await expect(page.locator('.levels-machine-group')).toContainText(['wolf-latitude','julian-minipc','julian-latitude','codecollective','compai','worker'])
 
 await expect(page.locator('#levels-legend')).not.toContainText('interface unavailable')
 await expect(page.locator('#levels-legend')).not.toContainText('interface unknown')
 await expect(page.locator('.levels-interface-link').first()).toHaveText('Link: 100 Mb/s · full duplex')
 await expect(page.locator('.levels-interface-link').nth(1)).toHaveText('Wi-Fi PHY: RX 72.2 Mb/s · TX 65 Mb/s')
 await expect(page.locator('#levels-legend')).not.toContainText('no-interface')
 await expect(page.getByRole('button',{name:/worker.local · enp9s0/})).toHaveCount(2)
 await expect(page.getByRole('button',{name:/worker.local · enp9s0/}).first()).toContainText('Not connected')
 await expect(page.getByRole('button',{name:/worker.local · eth0/})).toHaveCount(2)
 await expect(page.getByRole('button',{name:/worker.local · wlan0/})).toHaveCount(2)
 assert(paths.every(path=>(path.match(/L/g)||[]).length>=12),'Restored network history should contain all background samples')
 const send=page.getByRole('button',{name:/worker.local · eth0 · send/});
 await send.click();await expect(page.getByRole('button',{name:/worker.local · eth0 · send/})).toHaveAttribute('aria-pressed','false');
 await page.getByRole('button',{name:/worker.local · eth0 · send/}).click();
 for(const width of model.viewports){
  await page.setViewportSize({width,height:1000})
  const legend=page.locator('#levels-legend')
  const buttons=legend.locator('button')
  assert(await legend.locator('.levels-interface-link').evaluateAll(nodes=>nodes.every(el=>el.scrollWidth<=el.clientWidth)),`${width}: link speed text clipped`)
  assert(await legend.evaluate(el=>el.scrollWidth<=el.clientWidth),`${width}: machine groups overflow horizontally`)
  const groups=await legend.locator('.levels-network-machine').evaluateAll(nodes=>nodes.map(el=>{const r=el.getBoundingClientRect();return {top:r.top,bottom:r.bottom}}))
  assert(groups.every((r,n)=>n===0||r.top>=groups[n-1].bottom),`${width}: machine groups must stack vertically`)
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false,`${width}: page overflows`)
  for(let index=0;index<await buttons.count();index++){
   const button=buttons.nth(index);await button.scrollIntoViewIfNeeded()
   const geometry=await button.evaluate(el=>{
    const r=el.getBoundingClientRect(),panel=el.closest('#levels-legend').getBoundingClientRect(),hit=document.elementFromPoint(r.x+r.width/2,r.y+r.height/2)
    return {contained:r.left>=panel.left&&r.right<=panel.right,width:r.width,height:r.height,hit:!!hit&&el.contains(hit),textFits:el.scrollWidth<=el.clientWidth&&el.scrollHeight<=el.clientHeight}
   })
   assert(geometry.contained,`${width}: control ${index} clipped by panel`)
   assert(geometry.width>=model.minimumTargetPixels&&geometry.height>=model.minimumTargetPixels,`${width}: small target`)
   assert(geometry.hit,`${width}: control ${index} unreachable`)
   assert(geometry.textFits,`${width}: control text clipped`)
  }
  const toggle=page.getByRole('button',{name:/worker.local · eth0 · send/});await toggle.focus();await page.keyboard.press('Enter');await expect(toggle).toHaveAttribute('aria-pressed','false');await page.keyboard.press('Enter');await expect(toggle).toHaveAttribute('aria-pressed','true')
  review.checks.push({width,status:'passed',controls:await buttons.count()})
  await legend.evaluate(el=>el.scrollTop=0)
  await page.locator('#device-levels').screenshot({path:join(output,`network-${width}.png`)})
 }
 await page.getByLabel('Swarm resource usage').getByRole('button',{name:'Disk',exact:true}).click()
 await expect(page.getByRole('button',{name:/worker.local · sda · read/})).toContainText('Read · 60.0 MB/s')
 await expect(page.getByRole('button',{name:/worker.local · sda · read/})).toContainText('60.0%')
 await expect(page.getByRole('button',{name:/worker.local · sda · write/})).toContainText('40.0%')
 await expect(page.getByRole('button',{name:/worker.local · sdb · read/})).toContainText('Observing…')
 await expect(page.locator('#levels-scale')).toContainText('% of observed sustained capacity')
 for(const width of model.viewports){
  await page.setViewportSize({width,height:1000})
  const legend=page.locator('#levels-legend')
  assert(await legend.evaluate(el=>el.scrollWidth<=el.clientWidth),`${width}: disk cards clipped`)
  assert(await legend.locator('button').evaluateAll(nodes=>nodes.every(el=>el.scrollWidth<=el.clientWidth&&el.scrollHeight<=el.clientHeight)),`${width}: disk controls clipped`)
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false)
  const toggle=page.getByRole('button',{name:/worker.local · sda · read/});await toggle.focus();await page.keyboard.press('Enter');await expect(toggle).toHaveAttribute('aria-pressed','false');await page.keyboard.press('Enter');await expect(toggle).toHaveAttribute('aria-pressed','true')
  await legend.evaluate(el=>el.scrollTop=0)
  await page.locator('#device-levels').screenshot({path:join(output,`disk-${width}.png`)})
  review.checks.push({mode:'disk',width,status:'passed'})
 }
 await writeFile(join(output,'network-human-review.json'),JSON.stringify(review,null,2))
 assert.deepEqual(errors,[])
 console.log('PASS: directional network history renders continuous measured samples')
}finally{await browser.close();server.closeAllConnections();await new Promise(resolve=>server.close(resolve))}
