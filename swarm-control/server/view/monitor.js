import {snapshotFetch} from './snapshot-fetch.js';
import {currentPerformance,memorySeries,numeric,sortedRows,aggregateSwarm,SWARM} from './monitor-model.js';
const $=id=>document.getElementById(id),el=(tag,text)=>{const node=document.createElement(tag);if(text!==undefined)node.textContent=text;return node;};
const root=document.getElementById('monitor');
let data={hosts:[],history:[]},selected=SWARM,mode='Memory',frozen=false,filter='',sort=0,reverse=false,detail='Processes';
const details={Memory:['Processes','Counters'],CPU:['Logical CPUs'],GPU:['Devices','Clients','Metrics'],Disk:['Devices','Processes','Filesystems'],Network:['Interfaces','Connections'],LAN:['Neighbors']};
const tabs=['Memory','CPU','GPU','Disk','Network','LAN'];
const fmt=value=>numeric(value)?value.toLocaleString(undefined,{maximumFractionDigits:2}):'N/A';
function bytes(value,rate=false){if(!numeric(value))return 'N/A';let unit=0;for(;value>=1024&&unit<4;unit++)value/=1024;return fmt(value)+' '+['B','KiB','MiB','GiB','TiB'][unit]+(rate?'/s':'');}
const n=(v,unit='')=>numeric(v)?fmt(v)+unit:'N/A';
function card(title,value){const node=el('article');node.className='resource-card';node.append(el('small',title),el('strong',value));$('monitor-cards').append(node);}
function table(title,labels,rows,formats=[]){
 const target=$('monitor-table');target.append(el('h2',title));const input=el('input');input.type='search';input.placeholder='Filter rows…';input.value=filter;input.setAttribute('aria-label','Filter '+title);input.oninput=()=>{filter=input.value;const caret=input.selectionStart;paint();const next=document.querySelector('#monitor-table input');next?.focus();next?.setSelectionRange(caret,caret);};target.append(input);
 const wrap=el('div');wrap.className='monitor-table-wrap';const grid=el('table'),head=el('thead'),hr=el('tr');
 labels.forEach((label,index)=>{const th=el('th'),button=el('button',label+(index===sort?(reverse?' ↓':' ↑'):''));button.type='button';button.onclick=()=>{reverse=index===sort?!reverse:typeof rows.find(row=>row[index]!=null)?.[index]==='number';sort=index;paint();};th.append(button);hr.append(th);});head.append(hr);grid.append(head);
 const body=el('tbody');for(const row of sortedRows(rows.filter(row=>row.some(value=>String(value??'').toLowerCase().includes(filter.toLowerCase()))),Math.min(sort,labels.length-1),reverse)){
  const tr=el('tr');row.forEach((value,index)=>tr.append(el('td',formats[index]?formats[index](value):value??'N/A')));body.append(tr);
 }grid.append(body);wrap.append(grid);target.append(wrap);if(!rows.length)target.append(el('p','No current measurements for this section.'));
}
function charts(host){
 const series=memorySeries(host),frames=(data.history||[]).flatMap(frame=>(host?.aggregate?[aggregateSwarm(frame.hosts,frame.sampled_at)]:(frame.hosts||[]).filter(h=>h.host===host?.host)).map(h=>({host:h,time:frame.sampled_at})));
 for(const row of series){
  // Unavailable DRAM is explicit; do not rename cache events to bandwidth.
  if(row.value===null&&!row.key.startsWith('dram_'))continue;
  const box=el('article');box.className='resource-chart';box.append(el('h3',row.title),el('p',row.unit.startsWith('B/s')?bytes(row.value,true):n(row.value,' '+row.unit)));
  const points=frames.map(frame=>{const sample=memorySeries(frame.host,frame.time).find(v=>v.key===row.key);return {time:sample?.sampledAt,value:sample?.value};}).filter(p=>p.time&&p.time>=Date.now()/1000-300);
  const ns='http://www.w3.org/2000/svg',svg=document.createElementNS(ns,'svg');svg.setAttribute('viewBox','0 0 500 90');svg.setAttribute('role','img');svg.setAttribute('aria-label',row.title+' · independent five-minute scale');
  const max=Math.max(1,...points.map(p=>p.value??0)),now=Date.now()/1000;let segment=[],previous=null;
  const flush=()=>{if(!segment.length)return;const line=document.createElementNS(ns,'polyline');line.setAttribute('points',segment.join(' '));line.setAttribute('fill','none');line.setAttribute('stroke',row.key.includes('write')?'#edb66e':'#65d6c1');line.setAttribute('stroke-width','2');svg.append(line);segment=[];};
  for(const p of points){if(p.value==null||previous!==null&&p.time-previous>20)flush();if(p.value!=null)segment.push(`${500*(p.time-now+300)/300},${86-80*p.value/max}`);previous=p.time;}flush();box.append(svg,el('small',row.value===null?'Unavailable; no measured counter':'Independent scale · last five minutes'));$('monitor-charts').append(box);
 }
}
function paint(){
 const choose=$('monitor-detail');
 if(!details[mode].includes(detail))detail=details[mode][0];
 if([...choose.options].map(o=>o.value).join()!==details[mode].join())choose.replaceChildren(...details[mode].map(title=>{const option=el('option',title);option.value=title;return option;}));choose.value=detail;
 const host=selected===SWARM?aggregateSwarm(data.hosts):data.hosts.find(h=>h.host===selected),p=currentPerformance(host),m=p?.memory,now=Date.now()/1000;
 const cpuLive=host?.status==='live'&&now-host.sampled_at<15;
 $('monitor-tabs').replaceChildren(...tabs.map(title=>{const b=el('button',title);b.type='button';b.setAttribute('aria-pressed',String(mode===title));b.onclick=()=>{mode=title;detail=details[mode][0];filter='';sort=0;reverse=false;paint();};return b;}));
 $('monitor-fresh').textContent=(p?'Live · '+Math.max(0,Math.round(now-p.sampled_at))+'s ago':'Performance data unavailable / stale')+(frozen?' · frozen':'');$('monitor-fresh').classList.toggle('stale',!p);
 $('monitor-scope').textContent=host?.scope||p?.scope||'This node needs the built-in monitor update, or its collector is unavailable. Existing CPU/disk/interface observations remain available independently.';
 for(const id of ['monitor-cards','monitor-charts','monitor-table'])$(id).replaceChildren();
 $('monitor-notes').textContent=JSON.stringify({node:host?.host,limits:p?.limits,notes:p?.notes,source:p?'Authenticated enrolled-node telemetry':'Unavailable'},null,2);
 if(mode==='Memory'){
  card('RAM total',bytes(m?.total_bytes));card('Available',bytes(m?.available_bytes));card('RAM used',host?.aggregate?bytes(m?.used_bytes):numeric(m?.total_bytes)&&numeric(m?.available_bytes)?bytes(m.total_bytes-m.available_bytes):'N/A');card('Cached / reclaimable',bytes(m?.cached_bytes));card(host?.aggregate?'Highest node memory pressure · some / full (10 s)':'Memory pressure · some / full (10 s)',n(m?.pressure_avg10?.some,'%')+' / '+n(m?.pressure_avg10?.full,'%'));
  card('Backend',host?.aggregate?'Mixed node collectors':({1:'IMC domains',2:'Aggregate IMC',3:'Root-group MBM',4:'Cache / OS fallback'})[m?.backend_tier]||'N/A');card('Dirty / writeback',bytes(m?.dirty_bytes)+' / '+bytes(m?.writeback_bytes));charts(host);
  if(detail==='Counters')table('Memory / cache counter domains',['Counter','Value','Unit'],(m?.counters||[]).map(r=>[r.name,r.value,r.unit]));
  else table('Processes · RSS and storage I/O',['PID','Name','RSS','Storage read/s','Storage write/s','Read chars/s','Write chars/s'],(p?.processes||[]).map(r=>[r.pid,r.name,r.rss_bytes,r.read_bytes_per_second,r.write_bytes_per_second,r.read_chars_per_second,r.write_chars_per_second]),[null,null,v=>bytes(v),v=>bytes(v,true),v=>bytes(v,true),v=>bytes(v,true),v=>bytes(v,true)]);
 }else if(mode==='CPU'){
  card('CPU executing',cpuLive?n(host.percent,'%'):'N/A');card('Logical CPUs',host?.cpus??'N/A');if(host?.aggregate)card('CPU used · core equivalents',cpuLive&&numeric(host.percent)&&numeric(host.cpus)?n(host.percent*host.cpus/100)+' / '+fmt(host.cpus):'N/A');card('I/O wait',cpuLive?n(host.cpu_iowait_percent,'%'):'N/A');card('Stolen time',cpuLive?n(host.cpu_steal_percent,'%'):'N/A');
  table('Logical CPU utilization',['CPU','Executing %','I/O wait %','Steal %'],cpuLive?(host.logical_cpus||[]).map(r=>[r.id,r.percent,r.iowait_percent,r.steal_percent]):[],[null,v=>n(v,'%'),v=>n(v,'%'),v=>n(v,'%')]);
 }else if(mode==='GPU'){
  if(host?.aggregate){card('GPU devices',(host.hardware?.gpus||[]).length);card('GPU use · device equivalents',n(host.totals.gpu_busy)+' / '+host.totals.gpu_measured+' measured');card('VRAM used · measured GPUs',bytes(host.totals.vram_used));card('VRAM total · measured GPUs',bytes(host.totals.vram_total));}
  const live=host?.hardware&&!host.hardware.stale&&now-(host.hardware.sampled_at??host.sampled_at)<15;
  if(detail==='Clients')table('GPU client memory · shared buffers can overlap',['Device','Client','Owners','Resident','Allocated'],(p?.gpu?.clients||[]).map(r=>{const sum=prefix=>{const values=Object.entries(r.metrics).filter(([key,v])=>key.startsWith(prefix)&&numeric(v)).map(([,v])=>v);return values.length?values.reduce((a,b)=>a+b,0):null;};return [r.device,r.client,r.owners.map(o=>o.pid+' '+o.name).join(', '),sum('drm-resident-'),sum('drm-total-')];}),[null,null,null,v=>bytes(v),v=>bytes(v)]);
  else if(detail==='Metrics')table('GPU metrics',['Metric','Value','Unit'],Object.entries(p?.gpu?.rates||{}).map(([key,pair])=>[key,pair[0],pair[1]]));
  else table('GPU devices',['Name','Vendor','Busy %','VRAM total MiB','VRAM free MiB'],live?(host.hardware.gpus||[]).map(r=>[r.name,r.vendor,r.utilization_percent,r.memory_total_mib,r.memory_free_mib]):[]);
  const rates=p?.gpu?.rates||{};for(const [name,pair]of Object.entries(rates))card(name,n(pair[0],pair[1]==='%'?'%':' '+pair[1]));
  $('monitor-table').append(el('p','GPU-only memory read/write bytes: unavailable on i915 clients without directional counters. GPU requests are not byte bandwidth.'));
 }else if(mode==='Disk'){
  if(host?.aggregate){card('Total disk read',bytes(host.totals.disk_read,true));card('Total disk write',bytes(host.totals.disk_write,true));card('Disk space used · reported local filesystems',bytes(host.totals.disk_used));card('Disk space available',bytes(host.totals.disk_available));}
  if(detail==='Processes')table('Process storage I/O · kernel accounting',['PID','Name','Read/s','Write/s','Read chars/s','Write chars/s','Cancelled bytes'],(p?.processes||[]).map(r=>[r.pid,r.name,r.read_bytes_per_second,r.write_bytes_per_second,r.read_chars_per_second,r.write_chars_per_second,r.cancelled_write_bytes]),[null,null,v=>bytes(v,true),v=>bytes(v,true),v=>bytes(v,true),v=>bytes(v,true),v=>bytes(v)]);
  else if(detail==='Filesystems')table('Filesystems',['Device','Mount','Type','Total','Available'],(p?.filesystems||[]).map(r=>[r.device,r.mount,r.type,r.total_bytes,r.available_bytes]),[null,null,null,v=>bytes(v),v=>bytes(v)]);
  else table('Physical disk measurements',['Device','Status','Read/s','Write/s','Busy %','Capacity basis'],(host?.disks||[]).map(r=>{const fresh=now-r.sampled_at<15;return [r.name,fresh?r.status:'stale',fresh?r.read_bytes_per_second:null,fresh?r.write_bytes_per_second:null,fresh?r.busy_percent:null,r.capacity_basis??'unknown'];}),[null,null,v=>bytes(v,true),v=>bytes(v,true),v=>n(v,'%')]);
  for(const r of (host?.aggregate?[]:p?.filesystems||[]))card(r.mount,bytes(r.available_bytes)+' free / '+bytes(r.total_bytes));
 }else if(mode==='Network'){
  if(host?.aggregate){card('Total network receive',n(host.totals.network_rx,' Mb/s'));card('Total network send',n(host.totals.network_tx,' Mb/s'));}
  if(detail==='Connections')table('TCP / UDP endpoints · TCP rates are acknowledged / received payload',['Protocol','Local','Remote','RX/s','ACK TX/s','RTT ms','Receive queue','Send queue','State','Owners'],(p?.connections||[]).map(r=>[r.protocol,r.local,r.remote,r.rx_bytes_per_second,r.tx_acked_bytes_per_second,r.rtt,r.recvq,r.sendq,r.state,r.owners.map(o=>o.pid+' '+o.name).join(', ')]),[null,null,null,v=>bytes(v,true),v=>bytes(v,true),null,v=>bytes(v),v=>bytes(v)]);
  else table('Interfaces',['Name','State','Receive Mb/s','Send Mb/s','Link Mb/s'],(host?.network_interfaces||[]).map(r=>[r.name,r.state,now-r.sampled_at<15?r.rx_mbps:null,now-r.sampled_at<15?r.tx_mbps:null,r.link_speed_mbps]));
  for(const r of (host?.aggregate?[]:p?.connections||[]).slice(0,12))card(r.remote,'RX '+bytes(r.rx_bytes_per_second,true)+' · ACK TX '+bytes(r.tx_acked_bytes_per_second,true));
 }else{
  table('LAN neighbor cache · not a complete reachability inventory',['IP address','Interface','MAC','State','From node TCP/s','To node ACK TCP/s'],(p?.lan||[]).map(r=>{
   const flows=(p.connections||[]).filter(f=>(!host?.aggregate||f.node===r.node)&&f.protocol==='tcp'&&f.remote.replace(/^\[/,'').replace(/\]?:\d+$/,'').split('%')[0]===r.address);
   const sum=key=>flows.length&&flows.every(f=>numeric(f[key]))?flows.reduce((n,f)=>n+f[key],0):null;
   return [r.address,r.interface,r.mac,Array.isArray(r.state)?r.state.join('/'):r.state,sum('rx_bytes_per_second'),sum('tx_acked_bytes_per_second')];
  }),[null,null,null,null,v=>bytes(v,true),v=>bytes(v,true)]);
 }
}
$('monitor-detail').onchange=()=>{detail=$('monitor-detail').value;filter='';sort=0;reverse=false;paint();};
$('monitor-node').onchange=()=>{selected=$('monitor-node').value;filter='';paint();};
$('monitor-freeze').onclick=()=>{frozen=!frozen;$('monitor-freeze').textContent=frozen?'Resume':'Freeze';$('monitor-freeze').setAttribute('aria-pressed',String(frozen));paint();};
async function refresh(){
 if(document.hidden||frozen)return;
 try{
  const response=await snapshotFetch(root.dataset.source||'/api/resources',{cache:'no-store',signal:AbortSignal.timeout(9000)});if(!response.ok)throw Error('HTTP '+response.status);const payload=await response.json();data=root.dataset.source?(payload.resources||{hosts:[],history:[]}):payload;
  document.dispatchEvent(new CustomEvent('resource-context',{detail:data}));
  const names=[SWARM,...new Set((data.hosts||[]).map(h=>h.host))];if(!names.includes(selected))selected=SWARM;
  const select=$('monitor-node');if([...select.options].map(o=>o.value).join()!==names.join()){select.replaceChildren(...names.map(name=>{const o=el('option',name===SWARM?'Entire swarm':name);o.value=name;return o;}));}select.value=selected;paint();
 }catch(error){$('monitor-fresh').textContent='Collector unavailable: '+error.message;$('monitor-fresh').classList.add('stale');data={hosts:[],history:[]};document.dispatchEvent(new CustomEvent('resource-context',{detail:data}));paint();}
}
document.addEventListener('select-resource-host',event=>{selected=event.detail.host;const select=$('monitor-node');if([...select.options].some(o=>o.value===selected)){select.value=selected;filter='';paint();root.scrollIntoView({behavior:'smooth',block:'start'});}});
refresh();setInterval(refresh,5000);document.addEventListener('visibilitychange',()=>{if(!document.hidden)refresh();});
