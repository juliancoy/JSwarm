import {deviceColor} from './levels-model.js';
import {computerName} from './pipeline-colors.js';
import {statusClass} from './task-display.js';
import {machineActions,machineStateTags,actionDisabled} from './machine-controls.js';
import {lanes,laneFor,facts,currentItems,inspectorSections,byteSize,runTypes,runType,completedNewestFirst,usedGpu,technologyColor,executionGroup,arrivalPresentation,pipelineMachines,tileLabel} from './pipeline-model.js';
const $=id=>document.getElementById(id),node=(tag,text)=>{const n=document.createElement(tag);if(text!==undefined)n.textContent=text;return n;};
let infrastructure=[],items=[],enrolledMachines=[],paused=false,busy=false,selected=null,pinned=null,activeMachine=null,activeGpu=null,machineTimer=null,machinePinned=false,machineColors=new Map();
const boxes=new Map(),columns=new Map(),machineGroups=new Map();
for(const [id,title,caption] of lanes){const section=node('section');section.className='pipeline-lane status-'+id;const heading=node('h2',title),count=node('span','0');heading.append(count);section.append(heading,node('p',caption));section.lastChild.className='lane-caption';const grid=node('div');grid.className='shard-grid';section.append(grid);if(id==='running')grid.classList.add('running-groups');$('board').append(section);columns.set(id,{grid,count});}
function machineGroup(item){const host=computerName(item.worker)||'unassigned';let group=machineGroups.get(host);if(!group){const card=node('section');card.className='running-machine';card.tabIndex=0;card.setAttribute('role','button');card.setAttribute('aria-pressed','false');card.dataset.host=host;card.onpointerover=event=>{if(!event.target.closest('.shard,.executor-gpu')&&(activeMachine!==host||activeGpu))machineDetails(host);};card.onpointerleave=()=>{if(activeMachine===host&&!machinePinned&&!card.matches(':focus-visible'))close();};card.onfocus=()=>machineDetails(host);card.onblur=()=>{if(activeMachine===host&&!machinePinned&&!card.matches(':hover'))close();};card.onclick=event=>{if(!event.target.closest('.shard,.executor-gpu')){machineDetails(host,true);}};card.onkeydown=event=>{if(event.target===card&&['Enter',' '].includes(event.key)){event.preventDefault();machineDetails(host,true);}};card.setAttribute('aria-label',host==='unassigned'?'Unassigned machine':host);const heading=node('h3',host==='unassigned'?'Unassigned':host.replace(/\.local$/,'')),count=node('span');heading.append(count);const grid=node('div');grid.className='machine-executors';card.append(heading,grid);columns.get('running').grid.append(card);group={card,grid,count,executors:new Map()};machineGroups.set(host,group);}group.card.style.setProperty('--machine-color',machineColors.get(host)||'var(--muted)');const device=executionGroup(item);let executor=group.executors.get(device.key);if(!executor){const section=node('section');section.className='executor-section'+(device.gpu?' executor-gpu':'')+(device.key==='cache'?' executor-cache':'');if(device.gpu){section.tabIndex=0;section.setAttribute('role','button');section.setAttribute('aria-label',device.label+' details on '+host);section.onpointerover=event=>{if(!event.target.closest('.shard')&&(activeMachine!==host||activeGpu?.key!==device.key))machineDetails(host,false,device);};section.onpointerleave=()=>{if(activeGpu?.key===device.key&&!machinePinned&&!section.matches(':focus-visible'))close();};section.onfocus=()=>machineDetails(host,false,device);section.onblur=()=>{if(!machinePinned&&!section.matches(':hover'))close();};section.onclick=event=>{if(!event.target.closest('.shard')){event.stopPropagation();machineDetails(host,true,device);}};section.onkeydown=event=>{if(event.target===section&&['Enter',' '].includes(event.key)){event.preventDefault();event.stopPropagation();machineDetails(host,true,device);}};}const heading=node('h4',device.label),count=node('span');heading.append(count);const grid=node('div');grid.className='machine-shards';section.append(heading,grid);group.grid.append(section);executor={section,grid,count};group.executors.set(device.key,executor);}return executor.grid;}
function ioChart(chart){
 const figure=node('figure');figure.className='io-chart';figure.append(node('figcaption',chart.title));
 const entries=chart.entries,available=Array.isArray(entries),total=available?entries.reduce((sum,e)=>sum+e.bytes,0):0;
 const pie=node('div');pie.className='io-pie';pie.setAttribute('role','img');
 const legend=node('ul');let angle=0;const stops=[];
 if(total>0){for(const [i,entry] of entries.entries()){
  const color='hsl('+((i*137.508+205)%360)+' 65% 65%)',start=angle;angle+=entry.bytes/total*360;stops.push(color+' '+start+'deg '+angle+'deg');
  const row=node('li'),swatch=node('span');swatch.className='io-swatch';swatch.style.background=color;
  row.append(swatch,node('span',entry.endpoint),node('strong',byteSize(entry.bytes)+' · '+(entry.bytes/total*100).toFixed(1)+'%'));legend.append(row);
 }pie.style.background='conic-gradient('+stops.join(',')+')';}
 else {const empty=node('li',available?'No completed payload transfers':'Source/destination measurements unavailable');empty.className='io-empty';legend.append(empty);}
 pie.setAttribute('aria-label',chart.title+': '+(total>0?entries.map(e=>e.endpoint+' '+byteSize(e.bytes)).join(','):available?'0 bytes recorded':'not measured'));
 figure.append(pie,legend);return figure;
}
function details(item){if(machinePinned||pinned&&pinned!==item.id)return;activeGpu=null;activeMachine=null;machinePinned=false;clearTimeout(machineTimer);selected=item.id;$('inspector-hint').hidden=true;$('detail-title').textContent=item.name;
 const panel=$('detail');panel.className='shard-detail '+statusClass(item.status);panel.style.setProperty('--machine-color',machineColors.get(computerName(item.worker))||'var(--muted)');
 const dismiss=node('button','Close');dismiss.type='button';dismiss.className='inspector-close';dismiss.onclick=close;
 const badge=node('span',(item.superseded_by?'Previous attempt · ':'')+item.status.replaceAll('_',' ')+(pinned?' · Pinned':''));badge.className='inspector-status';
 const badges=node('div');badges.className='inspector-badges';badges.append(badge);
 if(laneFor(item)==='completed'){const host=computerName(item.worker),machine=node('span',host?host.replace(/\.local$/,''):'Machine not recorded');machine.className='inspector-machine';machine.setAttribute('aria-label','Machine: '+(host||'not recorded'));badges.append(machine);}
 const category=node('span',runTypes.find(([key])=>key===runType(item))[1]);category.className='run-badge run-'+runType(item);badges.append(category);
 const technologies=node('div');technologies.className='technology-badges';technologies.setAttribute('aria-label','Run technologies');
 for(const label of item.runtime?.technologies||[]){const chip=node('span',label);chip.className='technology-badge tech-'+technologyColor(label);technologies.append(chip);}
 if(!technologies.children.length){technologies.textContent='Technologies not recorded';technologies.classList.add('technologies-unknown');}
 const sections=inspectorSections(item,items).map(section=>{
  const card=node(section.disclosure?'details':'section');card.className='inspector-section';card.append(node(section.disclosure?'summary':'h3',section.title));
  if(section.metrics){const grid=node('div');grid.className='inspector-metrics';for(const [label,value,note] of section.metrics){const metric=node('div');metric.append(node('span',label),node('strong',value),node('small',note));grid.append(metric);}card.append(grid);}
  if(section.charts)for(const chart of section.charts)card.append(ioChart(chart));
  const list=node('dl');for(const [label,value,note] of section.rows){const row=node('div');row.append(node('dt',label),node('dd',String(value)));if(note)row.append(node('p',note));list.append(row);}card.append(list);return card;
 });$('detail-facts').replaceChildren(dismiss,badges,technologies,...sections);for(const [id,box] of boxes)box.setAttribute('aria-pressed',String(id===selected));}

const machineSnapshots=new Map();
const machineRequests=new Map();
async function sendMachineAction(host,action){
 if(machineRequests.get(host)?.pending)return;
 machinePinned=true;updatePins();machineRequests.set(host,{pending:true,message:'Sending '+action+' to '+host+'…'});
 drawMachine(host,machineSnapshots.get(host));
 try{
  const response=await fetch('/api/tasks/machine/control',{method:'POST',headers:{'Content-Type':'application/json','X-JSwarm-Control':'1'},body:JSON.stringify({host,action}),signal:AbortSignal.timeout(10000)});
  const value=await response.json();if(!response.ok)throw Error(value.error||'Command not accepted');
  machineRequests.set(host,{pending:false,message:'Command accepted; waiting for this node’s response.'});
 }catch(error){machineRequests.set(host,{pending:false,error:true,message:error.message||'Command not confirmed. Check node state before retrying.'});}
 if(activeMachine===host){clearTimeout(machineTimer);refreshMachine(host);}
}
function machineControlPanel(host,data){
 const section=node('section');section.className='inspector-section machine-controls';section.append(node('h3','Machine controls'));
 const buttons=node('div');buttons.className='machine-action-buttons';
 const request=machineRequests.get(host);
 for(const [action,label,title] of machineActions){const button=node('button',label);button.type='button';button.title=title;button.dataset.action=action;button.disabled=actionDisabled(action,data?.control,request?.pending);button.onclick=()=>sendMachineAction(host,action);buttons.append(button);}
 section.append(buttons);const feedback=node('p',(request?.pending||request?.error?request.message:data?.control?.action?.message||request?.message)||'Commands go to this machine’s authenticated JSwarm API.');feedback.setAttribute('role','status');section.append(feedback);
 return section;
}
function capacityBar(label,percent,note){
 const wrap=node('div');wrap.className='machine-capacity';const head=node('div');head.append(node('span',label),node('strong',Number.isFinite(percent)?percent.toFixed(1)+'% available':'Not measured'));wrap.append(head);
 const bar=node('div');bar.className='capacity-track';
 if(Number.isFinite(percent)){bar.setAttribute('role','meter');bar.setAttribute('aria-label',label+' available');bar.setAttribute('aria-valuemin','0');bar.setAttribute('aria-valuemax','100');bar.setAttribute('aria-valuenow',String(percent));const fill=node('span');fill.style.width=Math.min(100,Math.max(0,percent))+'%';bar.append(fill);}else bar.classList.add('capacity-unknown');
 wrap.append(bar,node('p',note));return wrap;
}
function cpuBars(cpu){
 const section=node('section');section.className='inspector-section';section.append(node('h3','CPU usage'));
 if(!cpu?.cores?.length){section.append(node('p','Per-CPU usage unavailable'));return section;}
 const grid=node('div');grid.className='cpu-bars';
 for(const core of cpu.cores){
  const row=node('div');row.className='cpu-row';const label=node('span','CPU '+core.id);label.className='cpu-label';
  const track=node('div');track.className='cpu-track';track.setAttribute('role','meter');track.setAttribute('aria-label','CPU '+core.id+' busy');track.setAttribute('aria-valuemin','0');track.setAttribute('aria-valuemax','100');track.setAttribute('aria-valuenow',String(core.busy_percent));
  for(const [key,style] of [['user_percent','cpu-user'],['system_percent','cpu-system'],['steal_percent','cpu-steal'],['iowait_percent','cpu-wait']]){const segment=node('span');segment.className=style;segment.style.width=Math.max(0,Math.min(100,core[key]||0))+'%';track.append(segment);}
  track.title=['User '+core.user_percent.toFixed(1)+'%','System '+core.system_percent.toFixed(1)+'%','I/O wait '+core.iowait_percent.toFixed(1)+'%','Steal '+core.steal_percent.toFixed(1)+'%'].join(' · ');
  const value=node('strong',core.busy_percent.toFixed(1)+'%');value.className='cpu-value';row.append(label,track,value);grid.append(row);
 }
 section.append(grid);const legend=node('p');legend.className='cpu-legend';for(const [text,style] of [['User','cpu-user'],['System','cpu-system'],['Steal','cpu-steal'],['I/O wait','cpu-wait']]){const entry=node('span',text);const swatch=node('i');swatch.className=style;entry.prepend(swatch);legend.append(entry);}section.append(legend,node('p','Busy = user + system + steal; I/O wait shown separately. '+(cpu.sampled_at?'Sampled '+new Date(cpu.sampled_at*1000).toLocaleTimeString()+' · ':'')+'0.3-second sample, refreshed about every second.'));return section;
}
function drawMachine(host,data){
 if(activeMachine!==host)return;
 $('inspector-hint').hidden=true;$('detail-title').textContent=host.replace(/\.local$/,'');
 const panel=$('detail');panel.className='shard-detail machine-detail';panel.style.setProperty('--machine-color',machineColors.get(host)||'var(--muted)');
 const badge=node('span','JSwarm · '+(data?.status||'loading')+(machinePinned?' · Pinned':''));badge.className='inspector-status';
 const badges=node('div');badges.className='inspector-badges';badges.append(badge);
 for(const tag of machineStateTags(data?.control)){const chip=node('span',tag.label);chip.className='technology-badge node-state-'+tag.tone;if(tag.title)chip.title=tag.title;badges.append(chip);}
 const content=[badges,machineControlPanel(host,data)];
 const notice=node('p',data?.error||(data?.status==='stale'?'Measurements are stale; waiting for fresh node telemetry.':'Measurements fetched from this machine’s JSwarm node.'));notice.className='inspector-hint';content.push(notice);
 if(activeGpu){
 $('detail-title').textContent=activeGpu.label;
 const names=JSON.parse(activeGpu.key.slice(4));
 const all=data?.gpus||[];const matched=all.filter(g=>names.some(n=>[g.name,g.uuid,g.pci_id].includes(n)));
 const gpus=matched.length?matched:all.length===1?all:[];
 content.push(node('p',host.replace(/\.local$/,'')));
 for(const gpu of gpus){const card=node('section');card.className='inspector-section';card.append(node('h3',gpu.name),capacityBar('GPU engine',gpu.available_percent,(gpu.status==='stale'?'Stale measurement · ':'Available engine headroom · ')+gpu.basis));if(gpu.memory)card.append(capacityBar('VRAM',gpu.memory.available_percent,byteSize(gpu.memory.available_bytes)+' available of '+byteSize(gpu.memory.total_bytes)));else card.append(node('p',gpu.memory_domain==='shared-system-memory'?'Uses shared system RAM; no separate VRAM pool':'VRAM measurement unavailable'));card.append(node('p',(gpu.sampled_at?'GPU observed '+new Date(gpu.sampled_at*1000).toLocaleTimeString():'GPU measurement time unavailable')+' · '+(gpu.status||'unknown')));
 const list=node('dl');for(const [label,value] of [['Vendor',gpu.vendor],['Backend',gpu.backend],['Device UUID',gpu.uuid],['PCI address',gpu.pci_id]]){if(value){const row=node('div');row.append(node('dt',label),node('dd',value));list.append(row);}}card.append(list);content.push(card);}
 if(!gpus.length)content.push(node('p',data?.status==='loading'?'Loading GPU measurements…':'This execution device could not be matched to node telemetry.'));
 const count=items.filter(i=>i.status==='running'&&computerName(i.worker)===host&&executionGroup(i).key===activeGpu.key).length;
 content.push(node('p',count+' running jobs in this GPU section'),node('p',data?.sampled_at?'Telemetry observed '+new Date(data.sampled_at*1000).toLocaleTimeString():'Telemetry not yet available'));
 const dismiss=node('button','Close');dismiss.type='button';dismiss.className='inspector-close';dismiss.onclick=close;$('detail-facts').replaceChildren(dismiss,...content);return;
 }
 const resources=node('section');resources.className='inspector-section';resources.append(node('h3','Available resources'));
 const capacity=(label,value)=>resources.append(capacityBar(label,value?.available_percent,value?byteSize(value.available_bytes)+' available of '+byteSize(value.total_bytes):'Not reported by this node'));
 capacity('RAM',data?.ram);
 for(const disk of data?.disks||[])capacity('Disk · '+disk.name,disk);
 if(!data?.disks?.length)capacity('Disk',null);
 for(const gpu of data?.gpus||[]){resources.append(capacityBar(gpu.name,gpu.available_percent,(gpu.status==='stale'?'Stale GPU measurement · ':'GPU engine headroom · ')+gpu.basis));if(gpu.memory)capacity('VRAM · '+gpu.name,gpu.memory);}
 if(!data?.gpus?.length)resources.append(node('p',data?.status==='loading'?'Loading GPUs…':'No GPUs reported by this node.'));
 content.push(cpuBars(data?.cpu),resources);
 const speed=data?.speed||{},network=node('section');network.className='inspector-section';network.append(node('h3','Tested speed'));
 network.append(node('p',(speed.source||'Coordinator')+' ↔ '+host));
 if(speed.status==='tested'){
  const values=node('dl');for(const [label,value] of [['To this node',speed.to_node_mbps],['From this node',speed.from_node_mbps]]){const row=node('div');row.append(node('dt',label),node('dd',value.toFixed(1)+' Mb/s'));values.append(row);}network.append(values,node('p','SHA-256 verified · '+new Date(speed.sampled_at*1000).toLocaleString()),node('p',speed.scope));
 }else network.append(node('p',speed.note||(speed.status==='testing'?'Testing with a small RAM-only transfer…':'Waiting for JSwarm connection…')));
 content.push(network);
 const runtime=node('section');runtime.className='inspector-section';runtime.append(node('h3','JSwarm node'));
 const values=node('dl');for(const [label,value] of [['Commit',data?.commit||'Not recorded in this node’s deployment'],['Source revision',data?.node_revision||'Not reported'],['Running time',Number.isFinite(data?.uptime_seconds)?Math.floor(data.uptime_seconds/86400)+'d '+Math.floor(data.uptime_seconds%86400/3600)+'h '+Math.floor(data.uptime_seconds%3600/60)+'m':'Not reported'],['Container',data?.node_container||'Not reported'],['Worker policy',data?.policy||'Not reported'],['Telemetry observed',data?.sampled_at?new Date(data.sampled_at*1000).toLocaleString():'Not reported']]){const row=node('div');row.append(node('dt',label),node('dd',value));values.append(row);}runtime.append(values);content.push(runtime);
 const dismiss=node('button','Close');dismiss.type='button';dismiss.className='inspector-close';dismiss.onclick=close;
 const focused=document.activeElement?.closest('[data-action]')?.dataset.action;
 $('detail-facts').replaceChildren(dismiss,...content);
 if(focused)$('detail-facts').querySelector('[data-action="'+focused+'"]')?.focus({preventScroll:true});
}
async function refreshMachine(host){
 if(activeMachine!==host||document.hidden)return;
 try{const response=await fetch('/api/tasks/machine?host='+encodeURIComponent(host),{cache:'no-store',signal:AbortSignal.timeout(8000)});if(!response.ok)throw Error();const value=await response.json();machineSnapshots.set(host,value);drawMachine(host,value);if(activeMachine===host)machineTimer=setTimeout(()=>refreshMachine(host),value.refreshing?500:1000);}
 catch{drawMachine(host,{...machineSnapshots.get(host),status:'unavailable',error:'Machine details unavailable; retained measurements may be stale.'});if(activeMachine===host)machineTimer=setTimeout(()=>refreshMachine(host),5000);}
}
function machineDetails(host,pin=false,gpu=null){
 if(host==='unassigned'||!pin&&(pinned||machinePinned))return;
 clearTimeout(machineTimer);activeMachine=host;activeGpu=gpu;machinePinned=pin;selected=null;pinned=null;
 for(const box of boxes.values())box.setAttribute('aria-pressed','false');
 updatePins();drawMachine(host,machineSnapshots.get(host)||{status:'loading'});machineTimer=setTimeout(()=>refreshMachine(host),180);
}
function updatePins(){for(const [host,group] of machineGroups)group.card.setAttribute('aria-pressed',String(machinePinned&&activeMachine===host));for(const chip of $('machine-key').querySelectorAll('[data-machine]'))chip.setAttribute('aria-pressed',String(machinePinned&&activeMachine===chip.dataset.machine));}
function close(){activeGpu=null;activeMachine=null;machinePinned=false;clearTimeout(machineTimer);selected=null;pinned=null;$('detail').className='shard-detail';$('detail-title').textContent='Shard details';$('detail-facts').replaceChildren();$('inspector-hint').hidden=false;updatePins();for(const box of boxes.values())box.setAttribute('aria-pressed','false');}
document.addEventListener('keydown',event=>{if(event.key==='Escape')close();});
function render(){const query=$('search').value.toLowerCase(),workflow=$('workflow').value,active=new Set(currentItems(items.filter(i=>i.kind!=='shared-cache')).map(i=>i.id));const visible=items.filter(i=>(i.kind==='shared-cache'||workflow==='all'||workflow==='active'&&active.has(i.id)||workflow===i.parent)&&(!query||[i.name,i.job_id,i.worker,i.parent].join(' ').toLowerCase().includes(query)));const computers=pipelineMachines(visible,enrolledMachines).filter(host=>!query||host.toLowerCase().includes(query)||visible.some(item=>computerName(item.worker)===host));for(const host of computers)machineGroup({worker:host});const kept=new Set(),counts=new Map(lanes.map(([id])=>[id,0]));
 for(const item of visible){kept.add(item.id);const lane=laneFor(item);if(!item.superseded_by)counts.set(lane,counts.get(lane)+1);let box=boxes.get(item.id);if(!box){box=node('button');box.type='button';box.dataset.id=item.id;box.onpointerenter=()=>details(box.item);box.onpointerleave=()=>{if(!pinned&&selected===box.item.id&&!box.matches(':focus-visible'))close();};box.onfocus=()=>details(box.item);box.onblur=()=>{if(!pinned&&selected===box.item.id&&!box.matches(':hover'))close();};box.onclick=()=>{machinePinned=false;pinned=box.item.id;details(box.item);updatePins();};boxes.set(item.id,box);}const changed=box.item&&box.item.status!==item.status;box.item=item;box.style.setProperty('--machine-color',machineColors.get(computerName(item.worker))||'var(--muted)');box.className='shard run-'+runType(item)+' '+statusClass(item.status)+(changed?' changed':'')+(usedGpu(item)?' gpu-used':'')+(!item.worker?' unassigned':'');box.textContent=item.kind==='shared-cache'?'C':item.kind==='memory-pressure-benchmark'&&item.resources?.memory_mb?(item.resources.memory_mb/1024).toLocaleString(undefined,{maximumFractionDigits:1})+'G':tileLabel(item);box.setAttribute('aria-label',item.name+' · '+runTypes.find(([key])=>key===runType(item))[1]+' · '+item.status+(usedGpu(item)?' · GPU used':'')+' · '+item.parent);box.setAttribute('aria-pressed',String(item.id===selected));box.removeAttribute('title');const target=item.superseded_by?$('benchmark-history-grid'):lane==='running'?machineGroup(item):columns.get(lane).grid;if(box.parentNode!==target)target.append(box);}
 let cursor=columns.get('completed').grid.firstElementChild;for(const item of completedNewestFirst(visible.filter(i=>!i.superseded_by))){const box=boxes.get(item.id);if(box!==cursor)columns.get('completed').grid.insertBefore(box,cursor);cursor=box.nextElementSibling;}
 const historical=visible.filter(i=>i.superseded_by);$('benchmark-history').hidden=!historical.length;$('benchmark-history-count').textContent='('+historical.length+')';
 for(const [id,box] of boxes)if(!kept.has(id)){box.remove();boxes.delete(id);}for(const [host,group] of machineGroups){let total=0;for(const [key,executor] of group.executors){const count=executor.grid.children.length;if(!count){executor.section.remove();group.executors.delete(key);}else{executor.count.textContent=count;total+=count;}}if(!total&&!computers.includes(host)){group.card.remove();machineGroups.delete(host);}else {group.count.textContent=total?String(total):'No jobs in view';group.card.classList.toggle('machine-idle',!total);group.card.style.order=total?'0':'1';}}for(const [id,{count}] of columns)count.textContent=counts.get(id).toLocaleString();$('summary').textContent=counts.get('running').toLocaleString()+' running · '+visible.filter(i=>['waiting_dependency','waiting_implementation'].includes(i.status)).length.toLocaleString()+' blocked · '+visible.filter(i=>i.kind!=='shared-cache').length.toLocaleString()+' / '+(items.length-infrastructure.length).toLocaleString()+' jobs · '+visible.filter(i=>i.kind==='shared-cache').length+' cache nodes';$('empty').hidden=visible.length>0;$('machine-key').replaceChildren(...computers.map(host=>{const chip=node('button',host.replace(/\.local$/,''));chip.type='button';chip.dataset.machine=host;chip.onclick=()=>machineDetails(host,true);chip.className='machine-chip';chip.style.setProperty('--machine-color',machineColors.get(host)||'var(--muted)');return chip;}),node('span','Dashed: unassigned'));updatePins();if(selected){const item=visible.find(i=>i.id===selected);if(item)details(item);else close();}}
async function refresh(){if(paused||busy||document.hidden)return;busy=true;try{const response=await fetch('/api/tasks/pipeline',{cache:'no-store',signal:AbortSignal.timeout(15000)});if(response.status===401){$('errors').replaceChildren(Object.assign(document.createElement('a'),{href:'/auth/login',textContent:'Sign in with PIdP to view and control the swarm'}));$('live-status').textContent='Sign-in required';return;}if(!response.ok)throw Error('Queue status unavailable');const data=await response.json();infrastructure=data.infrastructure||[];items=[...(data.items||[]),...infrastructure];enrolledMachines=data.machines||[];machineColors=new Map(enrolledMachines.map(host=>[host,deviceColor(host)]));const stats=data.runtime_summary;const arrival=arrivalPresentation(stats);$('runtime-estimate').textContent=arrival.value;$('arrival-range').textContent=arrival.range;$('arrival-coverage').textContent=arrival.coverage;$('runtime-estimate').title=stats?.scope||'';const value=$('workflow').value;const parents=[...new Set(items.filter(i=>i.kind!=='shared-cache').map(i=>i.parent))].sort();const options=[['active','Current workflows'],['all','All workflows'],...parents.map(p=>[p,p])];$('workflow').replaceChildren(...options.map(([v,label])=>{const o=node('option',label);o.value=v;return o;}));$('workflow').value=options.some(([v])=>v===value)?value:'active';render();$('live-status').textContent='Live · '+new Date(data.sampled_at*1000).toLocaleTimeString();$('errors').textContent=(data.errors||[]).join(' · ');}catch(error){$('errors').textContent=error.message+'; last observations retained.';$('live-status').textContent='Reconnecting…';$('arrival-coverage').textContent='Forecast is stale; reconnecting. Last arrival estimate retained.';}finally{busy=false;}}
$('search').oninput=render;$('workflow').onchange=render;$('pause').onclick=()=>{paused=!paused;$('pause').textContent=paused?'Resume updates':'Pause updates';if(paused)$('live-status').textContent='Paused';else refresh();};document.addEventListener('visibilitychange',()=>{if(!document.hidden)refresh();});refresh();setInterval(refresh,5000);

async function refreshComputerColors(){machineColors=new Map(enrolledMachines.map(host=>[host,deviceColor(host)]));render();}
refreshComputerColors();setInterval(refreshComputerColors,60000);

document.addEventListener('visibilitychange',()=>{if(!document.hidden&&activeMachine){clearTimeout(machineTimer);refreshMachine(activeMachine);}});
