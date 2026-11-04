import {machineJobs} from './task-machines.js';
import {taskExport} from './tasks-export.js';
import {copyJson} from './export-data.js';
import {statusClass,progressText,groupRows,readEta} from './task-display.js';
const el=(tag,text)=>{const n=document.createElement(tag);if(text!==undefined)n.textContent=text;return n;};
const $=id=>document.getElementById(id);
const previousPercent=new Map(),previousState=new Map(),openCards=new Map(),closedGroups=new Set();
let latest=null,paused=false,feed=null,polling=false,first=true,exporting=false;
const statusBoxes=[...document.querySelectorAll('[name=task-status]')];
try{const saved=JSON.parse(localStorage.getItem('swarm-task-statuses'));if(Array.isArray(saved))for(const box of statusBoxes)box.checked=saved.includes(box.value);}catch{}
function matches(row){const query=$('task-search').value.toLowerCase();return statusBoxes.some(box=>box.checked&&statusClass(row.status)==='status-'+box.value)&&(!query||JSON.stringify(row).toLowerCase().includes(query));}
function visible(row){if(statusClass(row.status)==='status-completed'&&!statusBoxes.some(box=>box.value==='completed'&&box.checked))return false;return matches(row)||(row.children||[]).some(visible);}
function badge(status){const labels={waiting_inputs:'Needs input',waiting_capacity:'Waiting for capacity',waiting_dependency:'Waiting for prerequisite'};const n=el('span',labels[status]||status||'Unknown');n.className='badge '+statusClass(status);return n;}
function pct(value){return Number.isFinite(value)?Math.max(0,Math.min(100,value)):null;}
function meter(value,label,eta){const n=el('div');n.className='progress-track';n.role='progressbar';n.setAttribute('aria-label',label);n.setAttribute('aria-valuemin','0');n.setAttribute('aria-valuemax','100');n.setAttribute('aria-valuenow',String(value??0));if(value==null)n.removeAttribute('aria-valuenow');const fill=el('div');fill.className='progress-fill';fill.style.width=(value??0)+'%';n.append(fill);if(eta){n.classList.add('task-meter');const text=el('span',eta.text);text.className='meter-label';n.append(text);n.setAttribute('aria-valuetext',(value==null?'Progress unmeasured':value+'%')+' · '+eta.text);}return n;}
function workerName(name,row){if(row.status==='waiting_inputs')return 'Required inputs missing';return name&&name!=='unassigned'?name.replaceAll('.local',''):row.children?.length?'Workers shown in steps':statusClass(row.status)==='status-completed'?'Verified evidence':row.status==='not scheduled'?'Not scheduled':'Awaiting worker';}
function renderTask(task,changes){
 const box=el('article');box.className='job-card '+statusClass(task.status);box.dataset.task=task.id;
 const header=el('div');header.className='job-header';header.append(el('h3',task.name),badge(task.status));box.append(header);
 const p=task.primary_progress||task.progress,percent=pct(p?.work_percent??p?.percent),observedAt=latest?.sampled_at||task.updated;
 const eta=readEta(task,observedAt),metrics=el('dl');metrics.className='task-facts';
 const fact=(label,value)=>{const item=el('div');item.append(el('dt',label),el('dd',value));metrics.append(item);};
 fact(task.primary_progress?'Input aligned':'Verified',percent==null?'Measuring':percent+'%');
 fact(task.primary_progress?'Batches complete':'Work items complete',p?String(p.completed)+(task.primary_progress?'':' / '+p.total):'—');
 fact('Estimate',eta.text.replace(' · rough observed rate','').replace('Alignment ETA ~','~'));
 box.append(metrics,meter(percent,(p?.label||'Verified completion')+': '+task.name));
 if(p?.work_percent!=null){const text=el('p',progressText(p));text.className='scan-summary';box.append(text);}
 if(task.bottleneck){const signal=el('div');signal.className='limiting-stage';signal.append(el('span','Limiting stage'),el('strong',task.bottleneck.label));box.append(signal);}
 if(task.stale){const warning=el('p','Awaiting a fresh worker report');warning.className='warning';box.append(warning);}
 let children=task.children||[];
 if(task.primary_progress){const workers=children.filter(c=>c.id?.startsWith('worker-'));children=children.filter(c=>!c.id?.startsWith('worker-'));if(workers.length)children.push({id:'workers',name:'Workers ('+workers.length+')',status:workers.some(c=>statusClass(c.status)==='status-running')?'running':'queued',children:workers,detail:'Expand a machine for current phase and resource measurements.'});}
 const rows=groupRows(children);
 if(rows.length||task.detail){
  const details=el('details');details.className='job-details';details.open=openCards.get(task.id)??false;details.ontoggle=()=>openCards.set(task.id,details.open);
  const summary=el('summary','Stages, workers and evidence');summary.dataset.focus='summary:'+task.id;details.append(summary);
  if(task.bottleneck){const evidence=el('p',task.bottleneck.evidence);evidence.className='job-description';details.append(evidence);}
  if(task.fleet_constraint){const fleet=el('p','Fleet: '+task.fleet_constraint);fleet.className='job-description';details.append(fleet);}
  if(task.phase_timing?.sample_count){
   const timing=el('section');timing.className='phase-timing';timing.setAttribute('aria-label','Measured batch phases');timing.append(el('h4','Typical mapper batch'));
   const labels={reference_setup:'Reference setup',mapping_pipeline:'Read mapping',mapper_cleanup:'Mapper cleanup'},seconds=task.phase_timing.median_seconds,total=Object.values(seconds).reduce((a,b)=>a+b,0);
   for(const [key,value] of Object.entries(seconds)){const row=el('div');row.className='phase-timing-row';row.append(el('span',labels[key]||key),meter(total?100*value/total:0,labels[key]),el('strong',value.toFixed(1)+' s'));timing.append(row);}
   timing.append(el('p',task.phase_timing.sample_count+' recent batches. '+task.phase_timing.scope));details.append(timing);
  }
  if(task.detail){const desc=el('p',task.detail);desc.className='job-description';details.append(desc);}
  const tree=el('div');tree.className='step-tree';
  function add(child,path){
   if(!visible(child))return null;const key=task.id+':'+path,branch=el('div');branch.className='step '+statusClass(child.status);
   const disclosure=el('details');disclosure.className='step-disclosure';disclosure.open=openCards.get(key)??false;disclosure.ontoggle=()=>openCards.set(key,disclosure.open);
   const summary=el('summary');summary.dataset.focus=key;const row=el('div');row.className='step-row';const name=el('div');name.className='step-name';name.append(el('span',child.name));
   const secondary=child.current_phase||child.waiting_label||((child.bottleneck?.confidence!=='unavailable')?child.bottleneck?.label:null);
   if(secondary&&statusClass(child.status)!=='status-completed'){const note=el('small',secondary);note.className='step-file';name.append(note);}
   if(statusClass(child.status)!=='status-completed'){const estimate=readEta(child,observedAt).text;if(estimate!==secondary){const note=el('small',estimate);note.className='step-file';name.append(note);}}
   const value=pct(child.progress?.work_percent??child.progress?.percent??child.percent),info=el('span',value==null?'':value+'%');info.className='step-progress';row.append(name,badge(child.status),info);summary.append(row);disclosure.append(summary);
   const content=el('div');content.className='step-content';
   const texts=[child.runtime_detail,child.sampled_at?'Worker sampled: '+new Date(child.sampled_at*1000).toLocaleTimeString():null,child.elapsed_seconds!=null?'Phase elapsed: '+Math.round(child.elapsed_seconds)+' s'+(child.timing_basis?' · '+child.timing_basis:''):null,child.cpu_percent!=null?'Worker CPU: '+child.cpu_percent.toFixed(1)+'%':null,child.rss_bytes!=null?'Worker RSS: '+Math.round(child.rss_bytes/1048576)+' MiB':null,child.producer_cpu_percent!=null?'Source preparation CPU: '+child.producer_cpu_percent.toFixed(1)+'%':null,child.bottleneck?.evidence,child.worker?'Worker: '+workerName(child.worker,child):null,child.detail,progressText(child),child.basis,child.input,child.evidence?'Evidence: '+child.evidence:null,child.waiting_for?.length?'Requires: '+child.waiting_for.join(', '):null];
   for(const text of texts)if(text)content.append(el('p',text));
   for(const [i,c] of (child.children||[]).entries()){const item=add(c,path+'/'+i);if(item)content.append(item);}
   if(!content.children.length)content.append(el('p','No additional measurements recorded.'));disclosure.append(content);branch.append(disclosure);return branch;
  }
  for(const [i,child] of rows.entries()){const item=add(child,String(i));if(item)tree.append(item);}details.append(tree);box.append(details);
 }
 return box;
}
function renderMachines(data){
 const cards=machineJobs(data.tasks).map(machine=>{
  const card=el('article');card.className='machine-card';card.append(el('h3',machine.name.replaceAll('.local','')));
  const jobs=machine.jobs.filter(visible),active=machine.jobs.filter(j=>statusClass(j.status)==='status-running');
  card.append(el('p',active.length+' reported active · '+machine.jobs.filter(j=>statusClass(j.status)==='status-completed').length+' completed'));
  for(const job of jobs){
   const item=el('div');item.className='machine-job';item.append(el('small',job.workflow),el('h4',job.name),badge(job.status));
   const p=job.progress||job,value=pct(p.work_percent??p.percent??job.percent);
   const label=job.shared?'Shared job · individual machine progress unavailable':progressText(p)||(value==null?'Progress not measured':value+'%');
   item.append(el('p',label));if(!job.shared&&value!=null)item.append(meter(value,job.name));
   const phase=job.current_phase||job.waiting_label||job.runtime_detail||job.detail;
   if(phase)item.append(el('p',phase));
   if(job.sampled_at)item.append(el('small','Worker report '+new Date(job.sampled_at*1000).toLocaleTimeString()));
   if(job.stale||job.runtime_stale)item.append(el('p','Awaiting fresh measurements'));
   card.append(item);
  }
  if(!jobs.length)card.append(el('p','No jobs match the selected filters.'));
  return card;
 });
 $('machine-jobs').replaceChildren(...cards);
}
function render(data,changes){if(!data)return;renderMachines(data);const focus=document.activeElement?.dataset.focus;const all=[...data.tasks,...data.planned];const finished=t=>statusClass(t.status)==='status-completed';const rank=t=>({'status-failed':0,'status-running':1,'status-queued':2}[statusClass(t.status)]??3);const active=data.tasks.filter(t=>!finished(t)).filter(visible).sort((a,b)=>rank(a)-rank(b)||(b.priority||0)-(a.priority||0));
 $('tasks').replaceChildren(...active.map(t=>renderTask(t,changes)));if(!active.length)$('tasks').append(el('p','No current jobs match this view.'));
 $('planned').replaceChildren(...data.planned.filter(t=>!finished(t)).filter(visible).map(t=>renderTask(t,changes)));$('planned-section').hidden=!$('planned').children.length;
 const completed=all.filter(finished).filter(visible);$('completed').replaceChildren(...completed.map(t=>renderTask(t,changes)));$('completed-count').textContent=completed.length;$('completed-section').hidden=!completed.length;
 $('overview').replaceChildren(...[['running','Running'],['queued','Waiting'],['failed','Need attention'],['completed','Completed']].map(([state,label])=>{const item=el('div');item.className='overview-item status-'+state;item.append(el('strong',String(all.filter(t=>statusClass(t.status)==='status-'+state||(state==='failed'&&t.failed_work_items>0)).length)),el('span',label));return item;}));
 $('updated').textContent='Updated '+new Date(data.sampled_at*1000).toLocaleTimeString();$('errors').textContent=data.errors.join(' · ');const retries=data.retry_jobs||[],pending=retries.filter(r=>['backoff','claiming','running'].includes(r.state)),recovered=retries.filter(r=>r.state==='completed'),attention=retries.filter(r=>['blocked','exhausted'].includes(r.state));
 $('retry-count').textContent=pending.length+' pending';$('retry-summary').textContent=`${pending.filter(r=>r.state==='running').length} active · ${pending.filter(r=>r.state!=='running').length} waiting · ${attention.length} need attention · ${recovered.length} recovered`;
 const retryStatus=r=>r.state==='completed'?'completed':r.state==='running'?'running':['blocked','exhausted'].includes(r.state)?'failed':'queued';
 const shown=retries.filter(r=>statusBoxes.some(box=>box.checked&&box.value===retryStatus(r)));
 $('retry-jobs').replaceChildren(...shown.map(r=>el('li',r.task+' · attempt '+r.attempts+'/3 · '+r.state+(r.next_due&&r.state==='backoff'?' · next '+new Date(r.next_due*1000).toLocaleTimeString():''))));if(!shown.length)$('retry-jobs').append(el('li','No retries match the selected statuses.'));
 if(focus)[...document.querySelectorAll('[data-focus]')].find(n=>n.dataset.focus===focus)?.focus({preventScroll:true});}
function accept(data){if(paused)return;const changes=new Set();
 function visit(task,row){const key=task.id+':'+row.id;const value=row.status+'|'+(row.percent??row.progress?.percent??'');if(!first&&previousState.has(key)&&previousState.get(key)!==value){changes.add(key);const line=el('li',new Date().toLocaleTimeString()+' · '+row.name+' → '+row.status+(row.percent!=null?' · '+row.percent+'%':''));line.className='new-activity';$('live-activity').prepend(line);while($('live-activity').children.length>30)$('live-activity').lastChild.remove();}previousState.set(key,value);for(const child of row.children||[])visit(task,child);}
 for(const task of [...data.tasks,...data.planned])visit(task,task);first=false;latest=data;$('copy-tasks-json').disabled=exporting;render(data,changes);}
async function refresh(){if(polling||paused)return;polling=true;try{const response=await fetch('/api/tasks',{cache:'no-store',signal:AbortSignal.timeout(15000)});if(!response.ok)throw Error('Task status unavailable');accept(await response.json());}catch(e){$('errors').textContent=e.message+'; displayed observations may be stale';}finally{polling=false;}}
function live(){feed?.close();if(paused)return;feed=new EventSource('/api/tasks/stream');feed.addEventListener('snapshot',event=>{accept(JSON.parse(event.data));$('live-status').className='';$('live-status').replaceChildren(el('span'));$('live-status').firstChild.className='live-dot';$('live-status').append('Live · updates every 2 seconds');});feed.onerror=()=>{$('live-status').className='live-offline';$('live-status').textContent='Reconnecting · last observations retained';};}
$('task-search').oninput=()=>{render(latest,new Set());if($('task-search').value.trim())$('completed-section').open=true;};for(const box of statusBoxes)box.onchange=()=>{try{localStorage.setItem('swarm-task-statuses',JSON.stringify(statusBoxes.filter(b=>b.checked).map(b=>b.value)));}catch{}render(latest,new Set());if(box.value==='completed'&&box.checked)$('completed-section').open=true;};$('pause-live').onclick=()=>{paused=!paused;$('pause-live').textContent=paused?'Resume live updates':'Pause live updates';if(paused){feed?.close();$('live-status').className='live-offline';$('live-status').textContent='Live updates paused';}else{void refresh();live();}};
void refresh();live();setInterval(refresh,15000);
let eventCursor=0;
async function history(reset=false){
 if(reset)eventCursor=0;
 try{
  const select=document.getElementById('event-run'),run=select.value;
  const response=await fetch('/api/tasks/events?after='+eventCursor+(run?'&run='+encodeURIComponent(run):''),{cache:'no-store'});
  if(!response.ok)throw Error('Event history unavailable');const data=await response.json();
  if(select.options.length===1)for(const item of data.runs){const option=el('option',item.name);option.value=item.id;select.append(option);}
  document.getElementById('event-summary').textContent=`${data.total_runs} recorded runs · ${data.total_events} events in this selection · cursor ${data.cursor}`;
  document.getElementById('event-list').replaceChildren(...data.events.map(event=>el('li',`#${event.id} · ${event.run_id} · ${event.source} · ${event.kind}${event.step?' · '+event.step:''}${event.status?' · '+event.status:''} · recorded ${new Date(event.recorded_at*1000).toLocaleString()}`)));
  eventCursor=data.cursor;document.getElementById('event-next').disabled=!data.events.length;
 }catch(error){document.getElementById('event-summary').textContent=error.message;}
}
document.getElementById('event-run').onchange=()=>void history(true);
document.getElementById('event-next').onclick=()=>void history();
void history();

$('copy-tasks-json').onclick=async()=>{
 if(!latest||exporting)return;exporting=true;
 const button=$('copy-tasks-json'),status=$('copy-tasks-status');button.disabled=true;
 try{
  const report=taskExport(latest,{paused,search:$('task-search').value,statuses:statusBoxes.filter(b=>b.checked).map(b=>b.value)});
  const text=JSON.stringify(report,null,2),copied=await copyJson(text,'swarm-tasks-for-llm.json');
  status.textContent=(copied?'JSON copied':'JSON ready to select')+' · '+Math.round(new Blob([text]).size/1024)+' KiB · all tasks included';
 }catch(error){status.textContent='Copy failed: '+error.message;}finally{exporting=false;button.disabled=false;}
};
