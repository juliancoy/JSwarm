import {readResourceSnapshots} from './resource-fetch.js';
import {recordResourceHistory} from './resource-history.js';
import {cpuCoverage,testCpuSummary,percent} from './cpu-report.js';
import {snapshotFetch} from './snapshot-fetch.js';
import {deviceLevels,deviceColor,compareNetworkRows,taskTransferLabel,mergeNetworkMeasurements} from './levels-model.js';
import {renderDockerDownloads} from './docker-downloads.js';
const root=document.getElementById('device-levels');
root.innerHTML=`<div class="panel-heading"><div><div class="eyebrow">ALL DEVICES · LIVE OVERLAY</div><h2 id="usage-title">Resource usage</h2><p id="levels-description" class="muted"></p></div><div class="levels-controls"><label>Measure <select id="levels-mode"><option value="network">Network</option><option value="disk">Disk</option><option value="ram">RAM</option><option value="cpu">CPU (%)</option><option value="gpu">GPU (%)</option><option value="vram">GPU memory (%)</option></select></label><label>Global telemetry <select id="levels-transport" disabled><option value="tcp">TCP preferred</option><option value="webrtc">WebRTC preferred</option></select></label><label class="levels-smoothing"><input id="levels-smooth" type="checkbox"> Smooth (10 s)</label><label class="levels-smoothing" id="levels-split-label"><input id="levels-split" type="checkbox"> Separate send / receive</label></div></div><p id="levels-transport-status" class="muted" role="status">Connecting to launcher transport settings…</p><div class="levels-workspace"><div class="levels-chart"><div class="levels-key"><span id="levels-scale"></span><span id="levels-status" role="status">Connecting…</span></div><div id="levels-plot" tabindex="0" aria-label="Overlaid device measurements"></div><div class="levels-time"><span id="levels-window">Collecting history…</span><span>Now</span></div></div><aside class="levels-devices"><h3>Devices <small>Click to show or hide</small></h3><div id="levels-legend"></div></aside></div><section id="levels-docker" aria-label="Docker image download context"></section><p id="levels-coverage" class="muted"></p>`;
const selector=document.getElementById('levels-mode'),histories={network:new Map(),disk:new Map(),ram:new Map(),cpu:new Map(),gpu:new Map(),vram:new Map(),split:new Map()},hidden=new Set();
let mode='cpu',latest=[],busy=false,lastData=null,smoothing=false,highlighted=null;
const transportControl=document.getElementById('levels-transport'),transportStatus=document.getElementById('levels-transport-status');
let savingTransport=false;
let previousSnapshots={};
const transportDescription=policy=>`All launchers prefer ${policy.transport==='tcp'?'TCP':'WebRTC'} and fall back to ${policy.transport==='tcp'?'WebRTC':'TCP'} if disconnected. Changes apply as launchers connect.`;
transportControl.onchange=async()=>{
 const previous=lastData?.[1]?.telemetry_policy;
 savingTransport=true;transportControl.disabled=true;transportStatus.textContent='Saving global transport preference…';
 try{
  const response=await fetch('/api/telemetry/transport',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({transport:transportControl.value}),signal:AbortSignal.timeout(8000)});
  if(!response.ok)throw Error(response.status===401?'Sign in to change the global transport preference.':'Could not save the global transport preference.');
  const policy=await response.json();
  if(lastData)lastData[1].telemetry_policy=policy;
  transportControl.value=policy.transport;transportStatus.textContent=transportDescription(policy);
 }catch(error){if(previous)transportControl.value=previous.transport;transportStatus.textContent=error.message;}
 finally{savingTransport=false;transportControl.disabled=!lastData?.[1]?.telemetry_policy;}
};
const smoothControl=document.getElementById('levels-smooth');
try{smoothing=localStorage.getItem('lan-map.levels.smooth')==='true';}catch{}
smoothControl.checked=smoothing;
smoothControl.onchange=()=>{smoothing=smoothControl.checked;try{localStorage.setItem('lan-map.levels.smooth',String(smoothing));}catch{}if(lastData)update(...lastData,false);};
const pairedNetwork=document.body.classList.contains('task-manager');
let split=pairedNetwork;
const splitControl=document.getElementById('levels-split');
try{if(!pairedNetwork)split=localStorage.getItem('lan-map.levels.split')==='true';}catch{}
splitControl.checked=split;
splitControl.onchange=()=>{split=splitControl.checked;try{localStorage.setItem('lan-map.levels.split',String(split));}catch{}if(lastData)update(...lastData,false);};
const historyMode=()=>mode==='network'&&split?'split':mode;
function rowsFor(data,metrics,metric,now=Date.now()){return metric==='split'?['send','receive'].flatMap(direction=>deviceLevels(data,metrics,direction,now).map(row=>({...row,deviceId:row.id,id:row.id+':'+direction,direction,name:row.name+' · '+direction}))):deviceLevels(data,metrics,metric,now);}
function legendButton(row,network=false){
 const button=document.createElement('button');button.type='button';button.className=network?'levels-network-toggle':'levels-device';
 button.dataset.seriesId=row.id;button.style.setProperty('--device-color',deviceColor(row.deviceId||row.id));button.setAttribute('aria-pressed',String(!hidden.has(row.id)));
 const amount=row.value===null?(row.unavailableLabel||'No data'):row.value.toFixed(1)+(mode==='network'?' Mb/s':'%');
 button.setAttribute('aria-label',`${row.name}: ${amount}${row.disk&&row.throughput!==null?' · '+row.throughput.toFixed(1)+' MB/s':''}`);button.title=row.basis;
 button.onmouseenter=button.onfocus=()=>{highlighted=row.id;draw();};
 button.onmouseleave=button.onblur=()=>{if(highlighted===row.id){highlighted=null;draw();}};
 const label=document.createElement('span');if(row.disk)label.style.whiteSpace='normal';label.textContent=network?(row.direction==='send'?'↑ Send':row.direction==='receive'?'↓ Receive':row.direction==='read'?'Read · '+(row.throughput===null?'No data':row.throughput.toFixed(1)+' MB/s'):row.direction==='write'?'Write · '+(row.throughput===null?'No data':row.throughput.toFixed(1)+' MB/s'):'Total'):row.name;
 const value=document.createElement('strong');value.textContent=amount;button.append(label,value);
 if(network&&mode==='network'&&row.value!==null){const meta=document.createElement('small');meta.className='levels-rate-meta';meta.textContent=(row.value/8).toFixed(1)+' MB/s';button.append(meta);if(row.utilization!==null&&row.utilization!==undefined){const utilization=document.createElement('small');utilization.className='levels-rate-meta';utilization.textContent=row.utilization.toFixed(1)+'% of link';button.append(utilization);}}
 button.onclick=()=>{hidden.has(row.id)?hidden.delete(row.id):hidden.add(row.id);update(...lastData,false);};
 return button;
}
const openHealth=new Map();
function networkLegend(legend){
 const machines=new Map();
 for(const row of latest){
  const machine=row.machine||'Gateway-only devices';
  if(!machines.has(machine))machines.set(machine,new Map());
  const key=row.deviceId||row.id,interfaces=machines.get(machine);
  if(!interfaces.has(key))interfaces.set(key,[]);interfaces.get(key).push(row);
 }
 const rate=rows=>rows[0].disk?rows.reduce((sum,row)=>sum+(row.throughput??0),0):rows[0].throughput??(rows.some(row=>row.value!==null)?rows.reduce((sum,row)=>sum+(row.value??0),0):-1);
 const total=interfaces=>Array.from(interfaces.values()).reduce((sum,rows)=>sum+Math.max(0,rate(rows)),0);
 for(const [machine,interfaces] of [...machines].sort((a,b)=>total(b[1])-total(a[1])||a[0].localeCompare(b[0]))){
  const group=document.createElement('section');group.className='levels-network-machine';
  const heading=document.createElement('h4');heading.className='levels-machine-group';heading.textContent=machine.replace(/\.local$/i,'');heading.title=machine;group.append(heading);
  for(const rows of [...interfaces.values()].sort((a,b)=>rate(b)-rate(a)||a[0].name.localeCompare(b[0].name))){
   const first=rows[0],card=document.createElement('div');card.className='levels-interface';card.style.setProperty('--device-color',deviceColor(first.deviceId||first.id));
   const name=first.disk||first.interface||first.name.replace(/ · (send|receive)$/,'').replace(/ · gateway only$/,'');
   const caption=document.createElement('div');caption.className='levels-interface-heading';
   const label=document.createElement('span');label.textContent=first.disk?name:/^(en|eth)/.test(name)?'Ethernet':/^(wl|wifi)/.test(name)?'Wi-Fi':/^(ww|ppp)/.test(name)?'Mobile':name;
   caption.append(label);if(first.interface||first.model){const code=document.createElement('code');code.textContent=first.model||name;code.title=first.model||name;caption.append(code);}card.append(caption);
   if(first.interface||first.disk){const link=document.createElement('p');link.className='levels-interface-link';link.textContent=first.linkLabel;link.title=first.disk?first.basis:'Current negotiated Ethernet speed or reported Wi-Fi PHY rates. Payload throughput is lower; Wi-Fi rates change with radio conditions.';card.append(link);}
   const controls=document.createElement('div');controls.className='levels-interface-directions';
   rows.sort((a,b)=>(['send','read'].includes(a.direction)?0:1)-(['send','read'].includes(b.direction)?0:1));
   for(const row of rows)controls.append(legendButton(row,true));card.append(controls);
   if(first.interface){
    const health=document.createElement('details');health.className='levels-interface-health';health.open=openHealth.get(first.deviceId||first.id)||false;
    if(first.health&&['rx_errors_delta','tx_errors_delta','rx_dropped_delta','tx_dropped_delta','carrier_changes_delta'].some(k=>Number.isFinite(first.health[k])&&first.health[k]>0))health.classList.add('has-events');
    health.ontoggle=()=>openHealth.set(first.deviceId||first.id,health.open);
    const summary=document.createElement('summary');summary.textContent=first.healthLabel;health.append(summary);
    const text=document.createElement('p');const h=first.health;
    const counter=(field)=>Number.isFinite(h?.[field])?h[field]:'Unavailable';
    text.textContent=`Current counter window: ${h?.window_seconds?.toFixed(1)??'unknown'} s. Since interface started — RX/TX errors: ${counter('rx_errors_total')} / ${counter('tx_errors_total')}; RX/TX drops: ${counter('rx_dropped_total')} / ${counter('tx_dropped_total')}; CRC errors: ${counter('rx_crc_errors_total')}; link down events: ${counter('carrier_down_count_total')}.`;
    if(h?.last_link_change_at)text.textContent+=' Last link transition observed '+new Date(h.last_link_change_at*1000).toLocaleTimeString()+'.';
    text.textContent+=' Interface traffic includes all applications. Ethernet utilization is separate per direction; Wi-Fi PHY rates are not usable payload capacity.';health.append(text);card.append(health);
   }
   group.append(card);
  }
  if(mode==='network'){const transfers=document.createElement('p');transfers.className='levels-task-transfers';const host=lastData?.[1]?.hosts?.find(h=>h.host===machine);transfers.textContent=host?taskTransferLabel(host.task_transfers||[]):'Task-transfer telemetry unavailable';transfers.title='Instrumented payload received by this machine, excluding unrelated traffic. Historical averages are not current throughput.';group.append(transfers);}
  legend.append(group);
 }
}

function displayPoints(points){
 if(!smoothing)return points;
 let average=null,previous=null;
 return points.map(p=>{
  if(p.value===null){average=null;previous=null;return p;}
  const elapsed=previous===null?null:p.time-previous;
  average=average===null||elapsed>10000?p.value:average+(1-Math.exp(-elapsed/10000))*(p.value-average);
  previous=p.time;return {...p,value:average};
 });
}
try{const saved=localStorage.getItem('lan-map.levels');if(['network','disk','ram','cpu','gpu','vram'].includes(saved))mode=saved;}catch{}
const resources=document.createElement('nav');resources.className='resource-tabs';resources.setAttribute('aria-label','Resource');
for(const [value,title]of [['cpu','CPU'],['gpu','GPU'],['vram','GPU memory'],['ram','Memory'],['disk','Disk'],['network','Network']]){const button=document.createElement('button');button.type='button';button.dataset.measure=value;button.textContent=title;button.onclick=()=>{selector.value=value;selector.dispatchEvent(new Event('change'));};resources.append(button);}
root.querySelector('.panel-heading').after(resources);
const cpuSummary=document.createElement('p');cpuSummary.id='levels-cpu-summary';cpuSummary.className='muted';cpuSummary.setAttribute('aria-label','CPU measurement scope');resources.after(cpuSummary);
selector.value=mode;
selector.onchange=()=>{mode=selector.value;try{localStorage.setItem('lan-map.levels',mode);}catch{}if(lastData)update(...lastData,false);};
function draw(){
 const now=Date.now(),history=new Map([...histories[historyMode()]].map(([id,points])=>[id,displayPoints(points)])),visible=latest.filter(r=>!hidden.has(r.id)).sort((a,b)=>Number(a.id===highlighted)-Number(b.id===highlighted));
 const starts=visible.flatMap(r=>(history.get(r.id)||[]).map(p=>p.time));
 const span=Math.min(300000,Math.max(3000,now-(starts.length?Math.min(...starts):now)));
 document.getElementById('levels-window').textContent=(span<60000?Math.ceil(span/1000)+' seconds':(span/60000).toFixed(1)+' minutes')+' ago'+(span<299000?' · building 5-minute history':'');
 const peak=mode==='disk'?Math.max(100,...visible.flatMap(r=>(history.get(r.id)||[]).map(p=>p.value||0))):mode==='network'?Math.max(1,...visible.flatMap(r=>(history.get(r.id)||[]).map(p=>p.value||0)))*1.1:100;
 for(const bar of root.querySelectorAll('.levels-throughput-fill')){const value=Number(bar.dataset.value);bar.style.width=(Math.min(1,value/peak)*100)+'%';bar.parentElement.title=(smoothing?'Smoothed throughput':'Current throughput')+' · shared scale 0–'+peak.toFixed(1)+' Mb/s';}
 const plot=document.getElementById('levels-plot'),paired=mode==='network'&&pairedNetwork;
 plot.classList.toggle('levels-network-pair',paired);plot.replaceChildren();
 for(const direction of paired?['send','receive']:[null]){
 const pane=document.createElement('section');pane.className='levels-direction-chart';
 if(paired){const heading=document.createElement('h3');heading.textContent=direction==='send'?'↑ Upload':'↓ Download';pane.append(heading);}
 if(paired)plot.append(pane);
 const chartWidth=Math.max(140,paired?pane.clientWidth:Math.max(280,plot.clientWidth)),plotWidth=chartWidth-68;
 const chartHeight=Math.max(180,paired?pane.clientHeight-34:plot.clientHeight),plotBottom=chartHeight-23,plotHeight=plotBottom-15;
 const ns='http://www.w3.org/2000/svg',svg=document.createElementNS(ns,'svg');svg.setAttribute('viewBox','0 0 '+chartWidth+' '+chartHeight);svg.setAttribute('preserveAspectRatio','none');svg.setAttribute('role','img');svg.setAttribute('aria-label',(direction==='send'?'Upload':direction==='receive'?'Download':mode)+' levels over five minutes');
 const add=(tag,attrs,text)=>{const e=document.createElementNS(ns,tag);for(const [k,v]of Object.entries(attrs))e.setAttribute(k,v);if(text)e.textContent=text;svg.append(e);return e;};
 for(let i=0;i<=4;i++){const y=15+i*plotHeight/4;add('line',{x1:58,x2:chartWidth-10,y1:y,y2:y,stroke:'#ffffff10'});add('text',{x:48,y:y+4,'text-anchor':'end',fill:'#91a4b2','font-size':14},(peak*(4-i)/4).toFixed(peak>10?0:1));}
 for(const row of visible.filter(row=>!direction||row.direction===direction)){const points=(history.get(row.id)||[]).filter(p=>p.time>=now-300000);let segment=[];
 const flush=()=>{if(!segment.length)return;const line=segment.map((p,i)=>(i?'L':'M')+p).join(' '),color=deviceColor(row.deviceId||row.id),active=row.id===highlighted;if(segment.length>1&&(!['network','disk'].includes(mode)||active))add('path',{d:line+' L'+segment.at(-1).split(',')[0]+','+plotBottom+' L'+segment[0].split(',')[0]+','+plotBottom+' Z',fill:color,'fill-opacity':active ? .45 : highlighted ? .03 : .10});if(segment.length===1){const [cx,cy]=segment[0].split(',');add('circle',{cx,cy,r:2,fill:color});}const path=add('path',{d:line,fill:'none',stroke:color,'stroke-width':active?3.5:2,'stroke-opacity':highlighted && !active ? .25 : 1,'stroke-dasharray':['receive','write'].includes(row.direction)?'6 4':'none','vector-effect':'non-scaling-stroke'});const title=document.createElementNS(ns,'title');title.textContent=row.name;path.append(title);segment=[];};
 let prev=null;for(const p of points){if(p.value===null||prev!==null&&p.time-prev>20000)flush();if(p.value!==null)segment.push((58+plotWidth*(p.time-now+span)/span).toFixed(2)+','+(plotBottom-plotHeight*p.value/peak).toFixed(2));prev=p.time;}flush();
 const last=points.at(-1);if(last?.value!==null&&last&&now-last.time<10000)add('circle',{cx:58+plotWidth*(last.time-now+span)/span,cy:plotBottom-plotHeight*last.value/peak,r:3,fill:deviceColor(row.deviceId||row.id)});
 }
 if(paired)pane.append(svg);else plot.append(svg);
 }
 document.getElementById('levels-scale').textContent=mode==='network'?(split?'Mb/s · upload / download · shared scale':'Mb/s · receive + send · shared auto-scale'):mode==='disk'?'% of observed sustained capacity · read solid / write dashed':mode==='gpu'?'% GPU busy':mode==='vram'?'% dedicated GPU memory used':mode==='cpu'?'% CPU busy · across all logical cores':'% RAM used · excludes available cache';
}
function update(data,metrics,record=true){
 if(!savingTransport){
  const policy=metrics.telemetry_policy;
  transportControl.disabled=!policy;
  if(policy){transportControl.value=policy.transport;transportStatus.textContent=transportDescription(policy);}
  else transportStatus.textContent='Global transport settings require the updated master launcher.';
 }
 cpuSummary.hidden=mode!=='cpu';
 const measured=(metrics.hosts||[]).filter(h=>h.status==='live'&&Number.isFinite(h.window_seconds));
 const windows=measured.map(h=>h.window_seconds),ages=measured.map(h=>h.sample_age_seconds).filter(Number.isFinite);
 cpuSummary.textContent=`Fleet CPU: ${percent(metrics.total_percent)} · ${cpuCoverage(metrics)}. ${testCpuSummary(metrics)}.`+(windows.length?` ${Math.min(...windows).toFixed(1)}–${Math.max(...windows).toFixed(1)}s averages`:'')+(ages.length?` · samples up to ${Math.max(...ages).toFixed(1)}s old`:'');
 for(const button of resources.children)button.setAttribute('aria-pressed',String(button.dataset.measure===mode));
 document.getElementById('usage-title').textContent={gpu:'GPU usage',vram:'GPU memory usage',cpu:'CPU usage',ram:'Memory usage',disk:'Disk activity',network:'Network throughput'}[mode];
 document.getElementById('levels-split-label').hidden=mode!=='network'||pairedNetwork;
 root.querySelector('.levels-devices h3').firstChild.nodeValue=mode==='network'?'Network interfaces ':mode==='disk'?'Physical disks ':'Devices ';
 renderDockerDownloads(document.getElementById('levels-docker'),data,mode);
 lastData=[data,metrics];latest=rowsFor(data,metrics,historyMode());const now=Date.now();
 if(record)recordResourceHistory(histories,data,metrics,now,rowsFor);
 document.getElementById('levels-description').textContent=mode==='gpu'||mode==='vram'?'Live GPU counters by device. Missing GPU backends remain unmeasured. Compute validation and runtime readiness are shown under Devices & tools.':mode==='network'?'Physical interfaces grouped by computer. Each color identifies one interface; loopback and virtual interfaces are excluded.':mode==='disk'?'Read and write share one observed sustained capacity per physical disk: the highest combined throughput sustained for at least 30 seconds under load. This is a workload-dependent baseline; short bursts can exceed 100%.':mode==='cpu'?'Executing CPU time across all logical CPUs, averaged over full sampling intervals. Fleet and test-active averages include all host processes. I/O wait and stolen time are separate; hover over a device for details.':'Memory in use over time. Available memory, including reclaimable cache, is excluded.';
 if(smoothing){latest=latest.map(row=>({...row,value:row.value===null?null:(displayPoints(histories[historyMode()].get(row.id)||[]).at(-1)?.value??row.value)}));document.getElementById('levels-description').textContent+=' Smoothing uses a 10-second exponential average; brief peaks are reduced.';}
 latest.sort((a,b)=>mode==='network'?compareNetworkRows(a,b):(b.value??-1)-(a.value??-1)||a.name.localeCompare(b.name));
 const legend=document.getElementById('levels-legend'),focused=document.activeElement?.dataset.seriesId,scrollTop=legend.scrollTop;legend.replaceChildren();
 if(['network','disk'].includes(mode))networkLegend(legend);else for(const row of latest)legend.append(legendButton(row));
 legend.scrollTop=scrollTop;if(focused)Array.from(legend.querySelectorAll('button')).find(button=>button.dataset.seriesId===focused)?.focus({preventScroll:true});
 if(mode==='network'&&split)document.getElementById('levels-description').textContent+=(pairedNetwork?' Upload and download have separate charts on the same scale.':' Send is solid; receive is dashed.')+' Toggle either direction in the sidebar.';
 const count=latest.filter(r=>r.value!==null).length;
 document.getElementById('levels-coverage').textContent=mode==='disk'?`${count} / ${latest.length} read/write percentages available · Throughput stays live while the observed baseline is learned under load. Five-minute background history.`:mode==='network'?`${count} / ${latest.length} ${split?'directions':'interfaces'} measured · Five-minute background history. Gateway-only measurements may omit switched traffic.`:count+' / '+latest.length+(mode==='network'&&split?' directional series measured. ':' devices measured. ')+(mode==='network'?'Hosts with telemetry report physical-interface totals; other devices use gateway-visible traffic, which excludes switched traffic. These scopes differ; values are not additive.':'CPU, GPU, disk, and RAM require live host telemetry. Devices without telemetry remain listed as “No data”.')+' Host history is collected by the coordinator for five minutes, even while this page is closed.';
 document.getElementById('levels-status').textContent='Updated '+new Date().toLocaleTimeString();draw();
}
async function refresh(){
 if(busy||document.hidden)return;busy=true;
 try{
  const read=async url=>{const response=await snapshotFetch(url,{cache:'no-store',signal:AbortSignal.timeout(15000)});if(!response.ok)throw Object.assign(Error('Measurements unavailable'),{status:response.status});return response.json();};
  if(root.dataset.source){
   const snapshot=await read(root.dataset.source);
   document.dispatchEvent(new CustomEvent('test-context',{detail:{run:snapshot.run}}));
   update(snapshot.network,mergeNetworkMeasurements(snapshot.metrics,snapshot.resources));
  }else{
   const result=await readResourceSnapshots(read,previousSnapshots);
   previousSnapshots=result.snapshots;
   const {network,metrics,resources}=result.snapshots;
   update(network||{devices:[]},mergeNetworkMeasurements(result.failed.includes('metrics')?(resources||metrics||{hosts:[]}):(metrics||resources||{hosts:[]}),resources||{hosts:[]}));
   if(result.failed.length)document.getElementById('levels-status').textContent='Retrying '+[...new Set(result.failed.map(key=>key==='network'?'device discovery':'resource readings'))].join(', ')+' · showing last samples with their original timestamps';
  }
 }catch(error){
  if(root.dataset.source)document.dispatchEvent(new CustomEvent('test-context',{detail:{run:null}}));
  if(error.status===401){
   transportControl.disabled=true;transportStatus.textContent='Sign in to manage global telemetry transport.';
   previousSnapshots={};lastData=null;latest=[];for(const history of Object.values(histories))history.clear();
   for(const id of ['levels-legend','levels-cpu-summary','levels-coverage','levels-docker'])document.getElementById(id).replaceChildren();
   draw();
  }else if(lastData)update(...lastData);
  document.getElementById('levels-status').textContent=error.status===401?'Sign in to Swarm Control to view your Task Manager.':'Disconnected · measurements unavailable · retrying';
 }finally{busy=false;}
}

setInterval(refresh,3000);document.addEventListener('visibilitychange',refresh);void refresh();

new ResizeObserver(()=>{if(lastData)draw();}).observe(document.getElementById("levels-plot"));
