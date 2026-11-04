import {computerContributions,rankComputers,duration} from './history-contributions.js';
const jobs=document.getElementById('jobs'),feedback=document.getElementById('feedback'),signIn=document.getElementById('sign-in');
const kind=document.getElementById('kind'),state=document.getElementById('state'),search=document.getElementById('search');
const active=new Set(['queued','preparing','running','stopping']);
const labels={passed:'Completed',reused:'Reused',running:'Running',queued:'Queued',preparing:'Preparing',stopping:'Stopping',stopped:'Stopped',failed:'Failed',partial:'Partial',interrupted:'Interrupted',timed_out:'Timed out',unavailable:'Unavailable'};
const el=(tag,text,className)=>{const node=document.createElement(tag);node.textContent=text;if(className)node.className=className;return node;};
let rows=[],busy=false,rankBy='completed_tasks';
function renderContributions(visible){
 const root=document.getElementById('contributions');root.replaceChildren();
 const computers=computerContributions(visible);
 if(!computers.length)return;
 root.append(el('h2','Computer contributions'));
 const taskLeader=rankComputers(computers,'completed_tasks')[0];
 root.append(el('p',`Most completed tasks: ${taskLeader.name} · ${taskLeader.completed_tasks}`));
 const measuredLeader=rankComputers(computers,'cpu_seconds').find(row=>Number.isFinite(row.cpu_seconds));
 const estimatedLeader=rankComputers(computers,'estimated_cpu_slot_seconds').find(row=>Number.isFinite(row.estimated_cpu_slot_seconds));
 if(measuredLeader)root.append(el('p',`Most recorded CPU time: ${measuredLeader.name} · ${duration(measuredLeader.cpu_seconds)}`));
 else if(estimatedLeader)root.append(el('p',`Most estimated CPU-slot time: ${estimatedLeader.name} · ${duration(estimatedLeader.estimated_cpu_slot_seconds)}`));
 root.append(el('p','For the runs matching your filters. Reused results are excluded from completed work and CPU totals.','history-note'));
 const label=el('label','Rank by '),select=document.createElement('select');select.setAttribute('aria-label','Rank computers by');
 for(const [value,title] of [['completed_tasks','Completed tasks'],['cpu_seconds','Measured CPU time'],['estimated_cpu_slot_seconds','Estimated CPU-slot time']]){const option=el('option',title);option.value=value;select.append(option);}
 select.value=rankBy;select.onchange=()=>{rankBy=select.value;renderContributions(visible);};label.append(select);root.append(label);
 const wrapper=el('div','','history-table-scroll'),table=document.createElement('table');table.className='history-contributions';
 const caption=el('caption',`Computers ranked by ${select.selectedOptions[0].textContent.toLowerCase()}`);table.append(caption);
 const header=document.createElement('tr');for(const title of ['Computer','Tasks completed','Measured CPU time','Estimated CPU-slot time']){const th=el('th',title);th.scope='col';header.append(th);}const head=document.createElement('thead');head.append(header);table.append(head);const body=document.createElement('tbody');
 for(const computer of rankComputers(computers,rankBy)){
  const row=document.createElement('tr'),name=el('th',computer.name);name.scope='row';row.append(name,el('td',String(computer.completed_tasks)),el('td',duration(computer.cpu_seconds)+(computer.cpu_measured_tasks?` · ${computer.cpu_measured_tasks}/${computer.executed_tasks} tasks measured`:'')),el('td',duration(computer.estimated_cpu_slot_seconds)+(computer.cpu_estimated_tasks?` · ${computer.cpu_estimated_tasks}/${computer.executed_tasks} tasks timed`:'')));body.append(row);
 }
 table.append(body);wrapper.append(table);root.append(wrapper,el('p','Measured CPU time is recorded worker process CPU time. CPU-slot estimates multiply task duration by allocated CPUs; they are not measured utilization. Missing data is shown as “Not recorded”. Task sizes vary.','history-note'));
}

function render(){
 const expanded=new Set([...jobs.querySelectorAll('details[open]')].map(node=>node.dataset.id));
 const visible=rows.filter(row=>(kind.value==='all'||row.kind===kind.value)&&(state.value==='all'||(state.value==='active')===active.has(row.status))&&`${row.title} ${row.run_id||''}`.toLowerCase().includes(search.value.trim().toLowerCase()));
 jobs.replaceChildren();renderContributions(visible);
 if(!visible.length){jobs.append(el('p',rows.length?'No runs match these filters.':'No workload history is recorded yet.'));return;}
 for(const row of visible){
  const card=el('article','','history-card'),heading=el('div','','history-heading');
  heading.append(el('span',row.kind==='genome'?'Genome analysis':'Test run','history-kind'),el('span',labels[row.status]||'Unknown','history-state'));card.append(heading,el('h2',row.title));
  const date=row.started_at||row.submitted_at;
  if(date){const time=el('time',(row.started_at?'Started ':'Queued ')+new Date(date).toLocaleString());time.dateTime=date;card.append(time);}
  if(row.finished_at)card.append(el('p','Finished '+new Date(row.finished_at).toLocaleString(),'history-date'));
  const progress=row.progress||{},total=progress.total,completed=progress.completed??0,failed=progress.failed??0;
  if(Number.isFinite(total)&&total>0){
   card.append(el('p',`${completed} / ${total} ${row.kind==='genome'?'batches':'tasks'} complete · ${failed} failed`));
   const bar=document.createElement('progress');bar.max=total;bar.value=Math.min(completed+failed,total);bar.setAttribute('aria-label',`Progress for ${row.title}`);card.append(bar);
  }else if(active.has(row.status))card.append(el('p','Waiting for task counts.'));
  if(row.machines?.length){
   const details=document.createElement('details');details.dataset.id=row.id;details.open=expanded.has(row.id);details.append(el('summary',`Machines and tasks (${row.machines.length})`));
   for(const machine of row.machines){const section=el('div','','history-machine');section.append(el('h3',`${machine.name} · ${labels[machine.status]||machine.status||'Unknown'}`));section.append(el('p',`${machine.completed_tasks??0} tasks completed · CPU: ${duration(machine.cpu_seconds)} · Estimated CPU-slot time: ${duration(machine.estimated_cpu_slot_seconds)}`));const list=document.createElement('ul');for(const title of machine.tasks||[])list.append(el('li',title));section.append(list);details.append(section);}card.append(details);
  }
  if(row.run_id)card.append(el('small','Run '+row.run_id,'history-id'));
  jobs.append(card);
 }
}
async function refresh(){
 if(busy)return;busy=true;document.getElementById('refresh').disabled=true;
 try{
  const response=await fetch('/api/v1/swarm/history',{cache:'no-store',signal:AbortSignal.timeout(15000)});
  if(response.status===401){rows=[];jobs.replaceChildren();document.getElementById('contributions').replaceChildren();signIn.hidden=false;feedback.textContent='Sign in to view workload history.';return;}
  if(!response.ok)throw Error('History is unavailable. Showing the last loaded entries; try Refresh.');
  const data=await response.json();rows=data.jobs||[];signIn.hidden=true;render();feedback.textContent=`${rows.length} recent runs · Updated ${new Date().toLocaleTimeString()}`;
 }catch(error){feedback.textContent=error.message;}
 finally{busy=false;document.getElementById('refresh').disabled=false;}
}
for(const control of [kind,state])control.addEventListener('change',render);search.addEventListener('input',render);
document.getElementById('refresh').onclick=refresh;
document.addEventListener('visibilitychange',()=>{if(!document.hidden)void refresh();});
setInterval(()=>{if(!document.hidden)void refresh();},5000);void refresh();
