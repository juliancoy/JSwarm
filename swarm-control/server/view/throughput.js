import {snapshotFetch} from './snapshot-fetch.js';
import {throughputSources} from './throughput-model.js';
import {deviceColor} from './levels-model.js';
import {processLabel,renderResources,attributionLabel} from './resource-context.js';
const root=document.getElementById('throughput-sources');
root.innerHTML=`<div class="panel-heading"><div><div class="eyebrow">TRAFFIC ATTRIBUTION · LIVE FLOW ACCOUNTING</div><h2>Where throughput comes from</h2><p class="muted">Ranked by measured IP bit rate, with endpoints and observation evidence.</p></div><div class="flow-controls"><label>Observation <select id="flow-scope"><option value="uplink">Internet uplink · all packets</option><option value="devices">Devices · before/after NAT</option></select></label><label>Group by <select id="flow-group"><option value="sender">Sender</option><option value="process">Host process</option></select></label><label>Direction <select id="flow-direction"><option value="download">Download · remote → device</option><option value="upload">Upload · device → remote</option></select></label><label>Device <select id="flow-device"><option value="">All devices</option></select></label></div></div><div class="flow-statistics"><div><small id="flow-total-label">Observed downloads</small><strong id="flow-total">—</strong></div><div><small>Active flow records</small><strong id="flow-count">—</strong></div><div><small>Capture interval</small><strong id="flow-interval">—</strong></div><div><small>Observation point</small><strong>compai</strong></div></div><div class="flow-heading"><h3 id="flow-ranking-title">Top remote senders</h3><span id="flow-status" role="status">Connecting…</span></div><p id="flow-reconciliation" class="muted"></p><details><summary>Uplink history · snapshots, not interval averages</summary><div id="flow-history"></div></details><div id="flow-ranking"></div><section id="flow-resources" aria-label="Local file and database context"></section><details class="flow-details"><summary>Inspect active flows · endpoints, transport, and recent process owners</summary><div class="table-scroll"><table class="flow-table"><thead><tr><th>Source → destination</th><th>Transport / host process</th><th>Current rate</th><th>Share¹</th><th>Retained bytes²</th><th>Observation</th></tr></thead><tbody id="flow-rows"></tbody></table></div></details><p id="flow-attribution" class="muted"></p><p id="flow-quality" class="muted"></p><p id="flow-footnote" class="muted"></p><details class="flow-method"><summary>Measurement method & coverage</summary><p>Rates count IP bytes, including IP headers, over the latest capture interval. LAN uploads are observed before NAT; downloads are observed after NAT. The uplink ledger includes unmatched packets; device-ledger host traffic requires an exact connected kernel socket match. These ledgers must never be added together. Each attributed packet is counted at one observation point.</p><p>¹ Shares use only the observed flows in the selected direction and device filter. This is not a percentage of your Internet connection. ² Retained bytes cover the flow record’s lifetime, up to five minutes idle; they are not bytes for the five-minute chart window. Switched LAN traffic, other gateways, capture loss, unconnected host UDP and missed short-lived sockets can be absent from the device ledger; they remain in the uplink ledger with unknown ownership.</p><p>PTR and ASN names describe DNS and network ownership. A hosting provider does not establish which application generated traffic. For compai, a recent exact socket-owner match can identify the local process. Script filenames identify local programs. Open files and database-port hints are process context, not a proven socket-to-file mapping. Other devices’ files, remote filenames and database names behind encrypted traffic remain unknown. Socket ownership is polled every 0.5 seconds and retained for five minutes with packet-time matching. Reused tuples with conflicting owners remain ambiguous. File snapshots update separately every three seconds; neither establishes ownership of the entire retained byte count. Very short connections can still be missed. This uses flow-accounting conventions described by <a href="https://www.rfc-editor.org/rfc/rfc7011.html" target="_blank" rel="noreferrer">IPFIX (RFC 7011)</a>; it is not an IPFIX exporter.</p></details>`;
let data=null,direction='download',device='',group='sender',scope='uplink',busy=false;
const $=id=>document.getElementById(id),el=(tag,text,cls)=>{const e=document.createElement(tag);e.textContent=text;if(cls)e.className=cls;return e;};
const rate=v=>v===null?'—':v<1?(v*1000).toFixed(1)+' kb/s':v.toFixed(2)+' Mb/s';
try{const saved=JSON.parse(localStorage.getItem('lan-map.throughput')||'{}');if(['sender','process'].includes(saved.group))group=saved.group;if(['download','upload'].includes(saved.direction))direction=saved.direction;if(['uplink','devices'].includes(saved.scope))scope=saved.scope;if(typeof saved.device==='string'&&/^(?:[0-9a-f]{2}:){5}[0-9a-f]{2}$/i.test(saved.device))device=saved.device;}catch{}
$('flow-scope').value=scope;$('flow-group').value=group;$('flow-direction').value=direction;
function save(){try{localStorage.setItem('lan-map.throughput',JSON.stringify({group,direction,device,scope}));}catch{}}
const bytes=n=>n>=1048576?(n/1048576).toFixed(2)+' MiB':(n/1024).toFixed(1)+' KiB';
function render(){
 if(!data)return;
 const focus=document.activeElement;
 $('flow-device').disabled=scope==='uplink';
 if(scope==='uplink')device='';
 if(focus!==$('flow-device')){const options=new Map((data.devices||[]).filter(d=>d.mac).map(d=>[d.mac,d.hostname||d.ip]));for(const f of data.link_traffic?.flows?.records||[])if(!options.has(f.device_mac))options.set(f.device_mac,f.local_ip);if(device&&!options.has(device))options.set(device,device);$('flow-device').replaceChildren(new Option('All devices',''));for(const [mac,name]of options)$('flow-device').append(new Option(name,mac));$('flow-device').value=device;}
 const model=throughputSources(data,direction,device,Date.now(),group,scope);
 $('flow-total-label').textContent=direction==='download'?'Observed downloads':'Observed uploads';$('flow-total').textContent=rate(model.total);$('flow-count').textContent=model.fresh?String(model.rows.length):'—';$('flow-interval').textContent=model.fresh&&Number.isFinite(model.sampleSeconds)?model.sampleSeconds.toFixed(2)+' s':'—';
 $('flow-ranking-title').textContent=group==='process'?'Host process attribution':direction==='download'?'Top remote senders':'Top local senders';
 $('flow-status').textContent=model.fresh?'Sample '+new Date(model.updated).toLocaleTimeString():'No fresh flow data';
 $('flow-ranking').replaceChildren();
 for(const [i,source]of model.sources.slice(0,8).entries()){const row=el('div','','flow-contributor'),bar=el('div','','flow-contribution');bar.style.width=(source.share*100)+'%';bar.style.background=deviceColor(source.id);const identity=el('div','','flow-identity');identity.append(el('strong',(i+1)+'. '+source.name),el('small',source.detail));const amount=el('div','','flow-amount');amount.append(el('strong',rate(source.rate)),el('small',(100*source.share).toFixed(1)+'% of observed '+(direction==='download'?'downloads':'uploads')));row.append(bar,identity,amount);$('flow-ranking').append(row);}
 if(!model.sources.length)$('flow-ranking').append(el('p',model.fresh?'No attributed traffic in this sample. This does not prove the device is idle.':'Flow measurements unavailable. Reconnecting…','muted'));
 renderResources($('flow-resources'),data,model.rows,device);
 $('flow-rows').replaceChildren();
 for(const f of model.rows.slice(0,30)){
  const localEndpoint=f.deviceName+(f.recent_processes?.length?' · '+f.recent_processes.map(p=>processLabel(p,data)).join(' / '):'');
  const tr=el('tr',''),endpoints=el('td',''),src=direction==='download'?f.remoteName:localEndpoint,dst=direction==='download'?localEndpoint:f.remoteName;
  endpoints.append(el('strong',src+' → '+dst),el('small',(direction==='download'?f.remote_ip+' → '+f.local_ip:f.local_ip+' → '+f.remote_ip)),el('small',f.provider+(f.asn?' · AS'+f.asn:'')));
  const protocol=f.protocol===6?'TCP':f.protocol===17?'UDP':'IP '+f.protocol;
  const transport=el('td',protocol);transport.append(el('small',f.local_port===null?'Ports unavailable':direction==='download'?f.remote_port+' → '+f.local_port:f.local_port+' → '+f.remote_port));
  transport.append(el('small',f.recent_processes?.length?f.recent_processes.map(p=>processLabel(p,data)).join(', '):'Process not attributed'));
  transport.append(el('small',attributionLabel(f)));
  if(f.protocol===6)transport.append(el('small','TCP payload '+rate(f[direction+'_tcp_payload_mbps']||0)+' · ACK-only IP '+rate(f[direction+'_tcp_ack_only_mbps']||0)));
  if(f.protocol===17)transport.append(el('small',f.remote_port===443?'UDP/443 · may be encrypted QUIC; payload purpose unknown':'UDP · payload purpose unknown'));
  if(f.process_sampled_at)transport.title='Socket ownership observed '+new Date(f.process_sampled_at*1000).toLocaleTimeString();
  const share=el('td',(100*f.rate/model.total).toFixed(1)+'%');
  const observation=el('td',f.interface);observation.append(el('small',f[direction+'_basis']||f.basis),el('small','Last seen '+new Date(f.last_seen).toLocaleTimeString()));observation.title='First seen '+new Date(f.first_seen).toLocaleString();
  tr.append(endpoints,transport,el('td',rate(f.rate)),share,el('td',bytes(f[direction+'_bytes']||0)),observation);$('flow-rows').append(tr);
 }
 const interfaces=data.link_traffic?.uplink_interfaces||[];
 const ledger=data.link_traffic?.uplink_flows?.records||[];
 $('flow-reconciliation').textContent=!model.fresh?'Uplink accounting unavailable.':interfaces.map(name=>{
  const nic=data.link_traffic?.interface_rates?.[name]?.[direction+'_mbps'];
  const observed=ledger.filter(f=>f.interface===name).reduce((n,f)=>n+(f[direction+'_mbps']||0),0);
  const owned=ledger.filter(f=>f.interface===name&&f.recent_processes?.length).reduce((n,f)=>n+(f[direction+'_mbps']||0),0);
  return name+' · interface '+(Number.isFinite(nic)?rate(nic):'unavailable')+' · captured Internet '+rate(observed)+' · process attributed '+rate(owned)+(observed?' ('+(100*owned/observed).toFixed(1)+'%)':'')+' · interface minus Internet '+(Number.isFinite(nic)?rate(Math.max(0,nic-observed)):'unavailable');
 }).join(' | ')+' · '+(data.link_traffic?.uplink_history?.length||0)+' retained samples (every 5 s, up to 30 min; memory only). · Interface counters include local traffic and link headers; capture uses IP bytes. Differences also include sampling, offload and capture loss. Unknown owners may be host, container or forwarded traffic. Ledgers are separate.';
 $('flow-history').replaceChildren();
 for(const sample of (data.link_traffic?.uplink_history||[]).slice().reverse().filter((_,i)=>i%12===0)){
  const rates=sample.internet_rates||{};
  $('flow-history').append(el('p',new Date(sample.updated).toLocaleTimeString()+' · captured Internet upload '+rate(rates.upload??0)+' · download '+rate(rates.download??0)));
 }
 const evidence=data.host_attribution,confidenceFresh=evidence?.status==='available'&&Date.now()/1000-evidence.sampled_at<3;
 $('flow-attribution').textContent=!model.fresh||!confidenceFresh?'Host process-attribution coverage unavailable.':
  'Device-ledger Compai download attribution (accepted flows only): '+(Number.isFinite(evidence.host_download_coverage_percent)?evidence.host_download_coverage_percent.toFixed(1)+'% of observed host download rate':'no observed host download rate')+
  ' · '+(evidence.counts?.current||0)+' live / '+(evidence.counts?.retained||0)+' retained / '+(evidence.counts?.ambiguous||0)+' ambiguous / '+(evidence.counts?.unknown||0)+' unmatched host flow records. Other devices are outside this process-coverage total.';
 const stats=data.link_traffic?.capture_statistics||{},reported=Object.values(stats).filter(s=>Number.isFinite(s.kernel_drops)),drops=reported.reduce((n,s)=>n+s.kernel_drops,0);
 $('flow-quality').textContent=!model.fresh?'Capture quality unavailable':reported.length?'Kernel capture drops this interval: '+drops+' · since collector start: '+(data.link_traffic.kernel_drops_since_start??'unknown')+' · excludes packets missed before capture and unmatched host sockets.':'Kernel capture-drop telemetry unavailable.';
 $('flow-quality').classList.toggle('flow-warning',model.fresh&&drops>0);
 $('flow-footnote').textContent=model.fresh?'Showing '+Math.min(model.rows.length,30)+' of '+model.rows.length+' active flow records; top '+Math.min(model.sources.length,8)+(group==='process'?' process groups. Shares are of observed ':' senders. Shares are of observed ')+(device?'selected-device ':'')+direction+' traffic only.'+(model.evictions?' '+model.evictions+' flow records evicted at the capture limit.':''):'No current rates or shares are shown while the flow feed is unavailable.';
}
$('flow-scope').onchange=()=>{scope=$('flow-scope').value;save();render();};
$('flow-group').onchange=()=>{group=$('flow-group').value;save();render();};
$('flow-direction').onchange=()=>{direction=$('flow-direction').value;save();render();};$('flow-device').onchange=()=>{device=$('flow-device').value;save();render();};
async function refresh(){if(busy||document.hidden)return;busy=true;try{const r=await snapshotFetch('/api/devices?resolve=0',{cache:'no-store',signal:AbortSignal.timeout(5000)});if(!r.ok)throw Error();data=await r.json();render();}catch{if(data){data={...data,link_traffic:null,process_resources:{status:'unavailable'}};render();}$('flow-status').textContent='Disconnected · retrying';}finally{busy=false;}}
setInterval(refresh,3000);document.addEventListener('visibilitychange',refresh);void refresh();
