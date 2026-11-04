import './monitor.js';
import './wgs-progress.js';
import './machine-connections.js';
const summary=document.getElementById('task-summary'),hosts=document.getElementById('task-hosts');
const element=(tag,text)=>{const node=document.createElement(tag);node.textContent=text;return node;};
let signature='',resources={hosts:[]};
const bytes=value=>typeof value==='number'&&Number.isFinite(value)?(value/1048576).toLocaleString(undefined,{maximumFractionDigits:1})+' MiB/s':'N/A';
function refreshResources(){for(const node of hosts.querySelectorAll('[data-resource-host]')){const host=resources.hosts?.find(h=>h.host===node.dataset.resourceHost),p=host?.performance,live=p&&!p.stale&&Date.now()/1000-p.sampled_at<15&&Date.now()/1000-p.sampled_at>=-5;node.textContent=live?'Machine DRAM read '+bytes(p.memory?.dram_read_bytes_per_second)+' · write '+bytes(p.memory?.dram_write_bytes_per_second)+' · '+p.processes.length+' observed processes':'Machine resources unavailable / stale';}}
document.addEventListener('resource-context',event=>{resources=event.detail;refreshResources();});
document.addEventListener('test-context',event=>{
 const run=event.detail?.run,next=JSON.stringify(run);if(next===signature)return;signature=next;
 const expanded=new Set([...hosts.querySelectorAll('details[open]')].map(node=>node.dataset.host));hosts.replaceChildren();
 if(!run){summary.textContent='Task activity unavailable. Sign in or wait for the connection to recover.';return;}
 summary.textContent=run.id?`${run.finished?'Recorded run':'Current run'}: ${run.status} · ${run.id}`:'No task run recorded yet.';
 for(const host of run.hosts||[]){
  const details=document.createElement('details');details.dataset.host=host.host;details.open=expanded.has(host.host);
  details.append(element('summary',`${host.host} · ${host.status} · ${host.jobs.length} tasks`));
  const measurements=element('p','');measurements.dataset.resourceHost=host.host;details.append(measurements);
  const inspect=element('button','Inspect machine resources');inspect.type='button';inspect.onclick=()=>document.dispatchEvent(new CustomEvent('select-resource-host',{detail:{host:host.host}}));details.append(inspect);
  for(const job of host.jobs){const row=element('p',`${job.name} · ${job.status}`);details.append(row);}
  hosts.append(details);
 }
 refreshResources();
});
