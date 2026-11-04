import {snapshotFetch} from './snapshot-fetch.js';
import {linkSample,topPeers} from './congestion-model.js';
const root=document.getElementById('congestion');
const series=new Map(), rtts=[];
let busy=false;
const make=(tag,text,cls)=>{const n=document.createElement(tag);if(text!==undefined)n.textContent=text;if(cls)n.className=cls;return n;};
const rate=v=>v===null?'—':v<1?(v*1000).toFixed(0)+' kb/s':v.toFixed(1)+' Mb/s';
root.innerHTML=`<div class="panel-heading"><div><div class="eyebrow">LIVE · NETWORK PRESSURE</div><h2>Where the network gets busy</h2><p class="muted">Directional traffic, link headroom, and response time. Observed on compai.</p></div><span id="congestion-live" role="status">Connecting…</span></div><div class="pressure-layout"><div id="pressure-links"></div><aside class="pressure-side"><span class="pressure-label">BROWSER → COMPAI</span><strong id="pressure-rtt">Measuring…</strong><div id="pressure-latency"></div><small id="pressure-baseline">Collecting a response-time baseline</small><p class="muted">HTTP round trip includes browser and server work. It measures this browser’s path, not Internet latency.</p></aside></div><div class="pressure-bottom"><div><h3>Busiest visible Ethernet devices</h3><div id="pressure-peers"></div></div><p class="muted pressure-note">High traffic alone does not prove congestion. Ethernet rates are negotiated ceilings; Wi-Fi PHY rates overstate usable throughput and share airtime. Internet capacity, packet loss, and queue delay are not measured here. Device bars exclude traffic that stays inside the switch.</p></div>`;
function chart(values,capacity,label){
 const ns='http://www.w3.org/2000/svg',svg=document.createElementNS(ns,'svg');svg.setAttribute('viewBox','0 0 600 70');svg.setAttribute('preserveAspectRatio','none');svg.setAttribute('role','img');svg.setAttribute('aria-label',label);
 const peak=Math.max(.1,...values.flatMap(s=>[s?.rx||0,s?.tx||0]));
 const y=v=>65-58*v/peak;
 for(const [key,color] of [['rx','#72dfcf'],['tx','#b4a3ff']]){
  let points=[];
  const flush=()=>{if(!points.length)return;const path=document.createElementNS(ns,'path');path.setAttribute('d',points.map((p,i)=>(i?'L':'M')+p).join(' '));path.setAttribute('fill','none');path.setAttribute('stroke',color);path.setAttribute('stroke-width','2');svg.append(path);points=[];};
  values.forEach((s,i)=>{if(s?.[key]===null||!s){flush();return;}points.push((600*(120-values.length+i)/119).toFixed(1)+','+y(s[key]).toFixed(1));});flush();
 }
 return {svg,peak};
}
function render(data,rtt){
 const ids=[...new Set([data.interface,data.upstream?.interface,...Object.keys(data.interface_traffic||{})].filter(Boolean))];
 const holder=document.getElementById('pressure-links');holder.replaceChildren();
 let anyFresh=false;
 for(const id of ids){
  const s=linkSample(data,id);anyFresh ||= s.fresh;
  if(!series.has(id))series.set(id,[]);const history=series.get(id);history.push(s);if(history.length>120)history.shift();
  const lane=make('article',undefined,'pressure-lane');
  const head=make('div',undefined,'pressure-lane-heading'),title=make('div');title.append(make('strong',id===data.interface?'Ethernet · LAN':id===data.upstream?.interface?'Wi-Fi · upstream':id),make('small',id));
  const state=make('span',s.state,'pressure-state');state.dataset.level=!s.fresh?'unknown':s.pressure>=.85?'high':s.pressure>=.5?'warm':'low';head.append(title,state);lane.append(head);
  const directions=make('div',undefined,'pressure-directions');
  for(const [key,name,color] of [['rx','Receive','#72dfcf'],['tx','Send','#b4a3ff']]){
   const direction=make('div'),caption=make('div',undefined,'pressure-rate');caption.append(make('span',name),make('strong',rate(s[key])));const track=make('div',undefined,'pressure-ribbon'),fill=make('span');const capacity=s[key+'Capacity'];const ratio=s[key]!==null&&capacity?s[key]/capacity:null;fill.style.width=ratio===null?'0%':Math.min(100,ratio*100)+'%';fill.style.background=color;track.append(fill);direction.append(caption,track,make('small',ratio===null?'Utilization unknown':(ratio*100).toFixed(1)+'% of '+rate(capacity)+' link rate'));directions.append(direction);
  }
  lane.append(directions);const c=chart(history,null,'Receive and send throughput over the last four minutes');lane.append(c.svg,make('small','Up to 120 samples (~4 min while visible) · scale '+rate(c.peak)+' · receive teal / send violet'));holder.append(lane);
 }
 document.getElementById('congestion-live').textContent=anyFresh?'Live · '+new Date().toLocaleTimeString():'Waiting for fresh traffic samples';
 if(rtt!==null){rtts.push(rtt);if(rtts.length>120)rtts.shift();}
 const sorted=[...rtts].sort((a,b)=>a-b),base=sorted[Math.floor(sorted.length*.1)];
 document.getElementById('pressure-rtt').textContent=rtt===null?'Probe unavailable':rtt.toFixed(0)+' ms';
 const latency=document.getElementById('pressure-latency');latency.replaceChildren(chart(rtts.map(v=>({rx:v,tx:null})),null,'Browser HTTP round-trip history in milliseconds').svg);
 document.getElementById('pressure-baseline').textContent=rtts.length<10?'Collecting baseline · '+rtts.length+' / 10 samples':'Session low baseline '+base.toFixed(0)+' ms · current '+(rtt===null?'unavailable':Math.max(0,rtt-base).toFixed(0)+' ms above baseline');
 const peers=document.getElementById('pressure-peers');peers.replaceChildren();const rows=topPeers(data),max=Math.max(.001,...rows.map(p=>p.rate));
 for(const p of rows){const row=make('div',undefined,'pressure-peer'),bar=make('i');bar.style.width=100*p.rate/max+'%';row.append(bar,make('span',p.name),make('strong',rate(p.rate)));peers.append(row);}
 if(!rows.length)peers.append(make('p','No fresh device traffic available.','muted'));
}
async function refresh(){
 if(busy||document.hidden)return;busy=true;
 try{
  const request=snapshotFetch('/api/devices?resolve=0',{cache:'no-store',signal:AbortSignal.timeout(5000)}).then(r=>{if(!r.ok)throw Error('Traffic unavailable');return r.json();}).then(data=>({data}),error=>({error}));
  const start=performance.now();let rtt=null;
  try{const r=await fetch('/api/health',{cache:'no-store',signal:AbortSignal.timeout(4000)});await r.arrayBuffer();if(r.ok)rtt=performance.now()-start;}catch{}
  const result=await request;if(result.error)throw result.error;render(result.data,rtt);
 }catch{document.getElementById('congestion-live').textContent='Offline · measurements unavailable';document.getElementById('pressure-links').replaceChildren(make('p','Traffic feed unavailable. Reconnecting…','muted'));document.getElementById('pressure-rtt').textContent='Unavailable';document.getElementById('pressure-latency').replaceChildren();document.getElementById('pressure-baseline').textContent='Waiting for connection';document.getElementById('pressure-peers').replaceChildren();}finally{busy=false;}
}
setInterval(refresh,2000);document.addEventListener('visibilitychange',refresh);void refresh();
