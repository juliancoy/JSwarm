import {cpuCoverage,hostCpuDetail,hostCpuContext,testCpuSummary} from './cpu-report.js';
import {snapshotFetch} from './snapshot-fetch.js';
import './breadcrumbs.js';
import './host-terminal.js';
import {hardwareLines,probeBrowserGPU} from './hardware.js';
const total=document.getElementById('cpu-total'),coverage=document.getElementById('cpu-coverage'),hosts=document.getElementById('cpu-hosts');
const active=document.createElement('span');active.id='cpu-active';coverage.after(active);
let busy=false;
async function refresh(){
 if(busy)return;busy=true;
 try{
  const response=await snapshotFetch('/api/cpu',{cache:'no-store'});
  if(!response.ok)throw Error('CPU monitoring unavailable');
  const data=await response.json();
  document.dispatchEvent(new CustomEvent('cpu-sample',{detail:data}));
  total.textContent=data.total_percent==null?'Unavailable':data.total_percent.toFixed(1)+'%';
  coverage.textContent=cpuCoverage(data);active.textContent=testCpuSummary(data);
  hosts.replaceChildren();
  for(const host of data.hosts){
   const item=document.createElement('div');item.className='cpu-host';item.dataset.terminalHost=host.host;item.tabIndex=0;item.setAttribute('aria-haspopup','dialog');
   const label=document.createElement('span');label.textContent=hostCpuDetail(host);
   const meter=document.createElement('meter');meter.min=0;meter.max=100;meter.value=host.percent??0;meter.setAttribute('aria-label',host.host+' CPU utilization');meter.hidden=host.percent==null;
   item.append(label,meter);const context=document.createElement('small');context.textContent=hostCpuContext(host);item.append(context);
   for(const line of hardwareLines(host.hardware)){const detail=document.createElement('small');detail.textContent=line;item.append(detail);}
   hosts.append(item);
  }
 }catch(error){total.textContent='Unavailable';coverage.textContent='CPU readout disconnected';active.textContent='Test activity unavailable';hosts.replaceChildren();}
 finally{busy=false;}
}
setInterval(()=>{if(document.visibilityState==='visible')void refresh();},5000);
document.addEventListener('visibilitychange',()=>{if(document.visibilityState==='visible')void refresh();});
void refresh();
const browserPanel=document.createElement('div');browserPanel.className='browser-gpu';
const browserStatus=document.createElement('span');browserStatus.textContent='This browser: '+(navigator.gpu?'WebGPU available; compute untested':'WebGPU unavailable in this context');
const button=document.createElement('button');button.type='button';button.textContent='Test this browser’s GPU';
button.addEventListener('click',async()=>{button.disabled=true;let timeout;try{const result=await Promise.race([probeBrowserGPU(),new Promise((_,reject)=>{timeout=setTimeout(()=>reject(Error('GPU check timed out')),10000);})]);browserStatus.textContent=result.status+': '+result.reason;}catch(error){browserStatus.textContent='GPU check failed: '+error.message;}finally{clearTimeout(timeout);button.disabled=false;}});
browserPanel.append(browserStatus,button);document.getElementById('cpu-readout').append(browserPanel);
