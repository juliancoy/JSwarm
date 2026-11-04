import {pollVisible} from './visible-poll.js';
import {groupWebsiteGraph} from './providers.js';
import {originName,changes,graph} from './website-model.js';
const $=id=>document.getElementById(id),canvas=$('graph'),ctx=canvas.getContext('2d');
let data,run,selected,layout=[],graphData,width=1,height=1,chosenRun='';
const bytes=value=>value>=1048576?(value/1048576).toFixed(2)+' MiB':value>=1024?(value/1024).toFixed(1)+' KiB':value+' B';
const ms=value=>Number.isFinite(value)&&value>0?Math.round(value)+' ms':'Unknown';
function el(tag,text,className){const node=document.createElement(tag);node.textContent=text;if(className)node.className=className;return node;}
function field(container,name,value){if(value===undefined||value===null)return;const node=el('div','','field');node.append(el('span',name),document.createTextNode(String(value)));container.append(node);}
function details(){
 const node=graphData?.nodes.find(node=>node.id===selected),container=$('details');container.replaceChildren();if(!node)return;
 container.append(el('h3',originName(node.id)));
 field(container,node.provider_group?'Provider group':'Origin',originName(node.id));
 if(node.provider_group)field(container,'Individual origins',node.members.map(n=>n.origin).join(' · '));field(container,'Observed requests',node.requests);
 field(container,'Known response-body transfer',bytes(node.bytes));field(container,'Requests with unknown body size',node.unknown_bytes);
 field(container,'Failed requests / HTTP errors',node.errors);field(container,'HTTP 401 / 403 responses',node.auth_challenges||0);field(container,'Excluded requests',node.blocked);field(container,'Browser-cancelled requests',node.cancelled||0);
 field(container,'Mean completed-request duration',ms(node.mean_duration_ms));
 field(container,'HTTP status counts',Object.entries(node.statuses).map(([status,count])=>status+': '+count).join(' · ')||'No response recorded');
 field(container,'Resource types',Object.entries(node.types).map(([type,count])=>type+': '+count).join(' · '));
 field(container,'Observed server IPs',node.ips.join(' · ')||'Unavailable');
 for(const ip of node.ips){const network=data.network_metadata?.[ip];if(network)field(container,'IP network · '+ip,[network.ptr,network.organization,network.asn?'AS'+network.asn:null].filter(Boolean).join(' · ')||network.status||network.ptr_status);}
 field(container,'Observation',new Date(run.observed).toLocaleString());field(container,'Collector / Chromium',run.collector_revision ? run.collector_revision+' / '+run.browser_version : 'Not recorded in this initial run');field(container,'Evidence','Fresh unauthenticated synthetic browser; requested origin, not inferred server ownership');
 const failures=run.requests.filter(row=>row.origin===node.id&&row.failure_code);
 field(container,'Transport failure / cancellation codes',[...new Set(failures.map(row=>row.failure_code))].join(' · ')||'None recorded');
 const links=graphData.edges.filter(edge=>edge.a===node.id||edge.b===node.id);
 for(const edge of links)field(container,edge.context?'Page context':'Recorded initiator relationship',originName(edge.a)+' → '+originName(edge.b)+' · '+edge.requests+' request events');
}
function render(){
 $('capture').disabled=data.running;$('capture').textContent=data.running?'Capturing…':'Capture both sites';
 $('error').textContent=data.error||'';
 run=chosenRun?data.selected:data.runs.find(r=>r.site===$('site').value);
 const focused=document.activeElement=== $('run');
 if(!focused){$('run').replaceChildren(el('option','Latest observation'));$('run').firstChild.value='';for(const item of data.history.filter(r=>r.site===$('site').value)){const option=el('option',new Date(item.observed).toLocaleString());option.value=item.id;$('run').append(option);}$('run').value=chosenRun;}
 if(!run){$('status').textContent=data.running?'Capturing fresh browser observations…':'No observations yet. Capture both sites to begin.';return;}
 $('status').textContent=(data.running?'Capture running · ':'')+'Observed '+new Date(run.observed).toLocaleString()+(run.navigation_error?' · Navigation: '+run.navigation_error:'');
 const s=run.summary,m=run.metrics;
 $('metrics').replaceChildren();
 for(const [name,value] of [['Requests',s.requests],['Requested origins',s.origins.length],['Known transfer',bytes(s.bytes)],['Failures / auth / excluded',s.errors+' / '+(s.auth_challenges||0)+' / '+s.blocked],['First contentful paint',ms(m.fcp_ms)],['Observed LCP',ms(m.lcp_observed_ms)]]){const card=el('div','','metric');card.append(el('span',name),el('strong',value));$('metrics').append(card);}
 graphData=$('group-providers').checked ? groupWebsiteGraph(graph(run)) : graph(run);if(!graphData.nodes.some(n=>n.id===selected))selected=graphData.root;
 $('origins').replaceChildren();for(const node of graphData.nodes){const button=el('button',originName(node.id)+' · '+node.requests+' requests','origin'+(selected===node.id?' active':''));button.onclick=()=>{selected=node.id;details();draw();};$('origins').append(button);}
 details();$('coverage').textContent=run.coverage+' Vantage: '+run.vantage;
 const history=data.history.filter(r=>r.site===run.site),index=history.findIndex(r=>r.id===run.id),previous=history[index+1];
 const delta=changes(s,previous?.summary),container=$('changes');container.replaceChildren();
 if(delta){field(container,'Newly observed origins',delta.added.map(originName).join(' · ')||'None');field(container,'Not observed this run',delta.absent.map(originName).join(' · ')||'None');field(container,'Request / known-byte change',(delta.requests>=0?'+':'')+delta.requests+' requests · '+(delta.bytes>=0?'+':'−')+bytes(Math.abs(delta.bytes)));}
 else container.append(el('p','First available observation: no preceding run to compare.'));
 $('history').replaceChildren();for(const item of history){const row=el('div','','history-row'),button=el('button',new Date(item.observed).toLocaleString());button.onclick=()=>{chosenRun=String(item.id);pollOnce();};row.append(button,document.createTextNode(' · '+item.summary.requests+' requests · '+bytes(item.summary.bytes)+' · '+item.summary.errors+' failures'));$('history').append(row);}
 draw();document.body.dataset.ready='true';
}
function draw(){
 const rect=canvas.getBoundingClientRect();width=rect.width;height=rect.height;const scale=Math.min(devicePixelRatio||1,2);canvas.width=width*scale;canvas.height=height*scale;ctx.setTransform(scale,0,0,scale,0,0);ctx.clearRect(0,0,width,height);if(!graphData)return;
 const others=graphData.nodes.filter(n=>n.id!==graphData.root),center={x:width/2,y:height/2-10};
 layout=graphData.nodes.map(node=>{const i=others.findIndex(n=>n.id===node.id),angle=i/Math.max(1,others.length)*Math.PI*2-Math.PI/2;return {...node,x:i<0?center.x:center.x+Math.cos(angle)*Math.max(70,width*.32),y:i<0?center.y:center.y+Math.sin(angle)*height*.32,r:8+Math.min(16,Math.log10(1+node.bytes/1024)*4)};});
 for(const edge of graphData.edges){const a=layout.find(n=>n.id===edge.a),b=layout.find(n=>n.id===edge.b);ctx.strokeStyle=b.errors?'#f18c7f':'#4d8191';ctx.globalAlpha=edge.context ? .35 : .7;ctx.lineWidth=1+Math.min(9,Math.log2(1+edge.requests));ctx.setLineDash(edge.context?[4,7]:[]);ctx.beginPath();ctx.moveTo(a.x,a.y);ctx.lineTo(b.x,b.y);ctx.stroke();}ctx.setLineDash([]);ctx.globalAlpha=1;
 for(const node of layout){ctx.fillStyle=node.id===graphData.root?'#ae9fe5':'#99d9be';ctx.beginPath();ctx.arc(node.x,node.y,node.r,0,Math.PI*2);ctx.fill();ctx.strokeStyle=node.errors?'#f18c7f':Object.keys(node.statuses).length?'#73d5a0':'#71818d';ctx.lineWidth=2;ctx.beginPath();ctx.arc(node.x,node.y,node.r+5,0,Math.PI*2);ctx.stroke();if(node.id===selected){ctx.strokeStyle='#fff';ctx.beginPath();ctx.arc(node.x,node.y,node.r+10,0,Math.PI*2);ctx.stroke();}ctx.font='12px system-ui';ctx.textAlign='center';ctx.fillStyle='#e4eef3';ctx.fillText(originName(node.id),node.x,node.y+node.r+24,Math.min(200,width*.44));ctx.font='10px system-ui';ctx.fillStyle='#9bafbc';ctx.fillText(node.requests+' requests · '+bytes(node.bytes),node.x,node.y+node.r+40,180);}
}
canvas.onclick=event=>{const rect=canvas.getBoundingClientRect(),x=event.clientX-rect.left,y=event.clientY-rect.top;const node=layout.find(node=>Math.hypot(node.x-x,node.y-y)<node.r+12);if(node){selected=node.id;details();draw();}};
new ResizeObserver(draw).observe(canvas);
async function pollOnce(){try{const response=await fetch('/api/websites'+(chosenRun?'?run='+chosenRun:''));if(!response.ok)throw Error('HTTP '+response.status);data=await response.json();render();}catch(error){$('error').textContent=error.message;}}
$('group-providers').onchange=()=>{selected=null;if(data)render();};
$('site').onchange=()=>{chosenRun='';selected=null;pollOnce();};$('run').onchange=event=>{chosenRun=event.target.value;selected=null;pollOnce();};
$('capture').onclick=async()=>{ $('capture').disabled=true;try{const response=await fetch('/api/websites/capture',{method:'POST'});if(!response.ok&&response.status!==409)throw Error('HTTP '+response.status);await pollOnce();}catch(error){$('error').textContent=error.message;$('capture').disabled=false;}};
pollVisible(pollOnce,3000);
