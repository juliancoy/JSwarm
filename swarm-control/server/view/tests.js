import {renderOverallProgress} from './overall-progress.js';
import {summarizeProgress} from './run-progress.js';
import {authorizedAction} from './authorized-action.js';
import {snapshotFetch} from './snapshot-fetch.js';
import {renderVisualArtifacts} from './visual-artifacts.js';
import {renderActivity} from './run-activity.js';
import {renderRunWork} from './run-work.js';
import './machine-heat.js';
import './host-terminal.js';
import {summarizeFailure} from './failure-summary.js';
import {renderFailures} from './failure-display.js';
const $=id=>document.getElementById(id);
const terminal=new Set(['reused','partial','passed','failed','unavailable','timed_out','interrupted','stopped']);
const suiteEntries=host=>Object.entries(host.jobs||{}).sort(([,a],[,b])=>Number(terminal.has(a.status))-Number(terminal.has(b.status)));
let liveData=null;
let data=null,selected=new URLSearchParams(location.search).get('host')||null,chosen=new URLSearchParams(location.search).get('run')||'',polling=false;
let terminalChoice='';
const historyReports=new Map(),historyPending=new Set();
let historyLoading=false;
function historyStamp(item){return [item.status,item.finished||'',item.passed,item.reused,item.failed,item.suites].join('|');}
async function loadHistoryReasons(){
 if(historyLoading)return;historyLoading=true;
 try{
  const wanted=(data?.history||[]).slice(0,10).filter(item=>item.id!==data?.run?.id&&!historyPending.has(item.id)&&historyReports.get(item.id)?.stamp!==historyStamp(item));
  for(let i=0;i<wanted.length;i+=2){
   await Promise.all(wanted.slice(i,i+2).map(async item=>{
    historyPending.add(item.id);
    try{const response=await snapshotFetch('/api/distributed-tests?id='+encodeURIComponent(item.id),{cache:'no-store',signal:AbortSignal.timeout(5000)});if(!response.ok)throw Error();const result=await response.json();if(result.run)historyReports.set(item.id,{stamp:historyStamp(item),summary:summarizeFailure(result.run),work:result.run.hosts?.map(host=>({host:host.host,jobs:host.jobs}))});}
    catch{historyReports.set(item.id,{stamp:historyStamp(item),unavailable:true});}
    finally{historyPending.delete(item.id);}
   }));
   renderHistory();
  }
  const retained=new Set((data?.history||[]).slice(0,10).map(item=>item.id));for(const id of historyReports.keys())if(!retained.has(id))historyReports.delete(id);
 }finally{historyLoading=false;}
}
function inspectRecordedFailure(target){
 selected=target.host;terminalChoice=target.suite?target.host+'/'+target.suite:'';render();
 const pane=target.suite?$('live-terminal'):$('jobs');pane.scrollIntoView({block:'center',behavior:matchMedia('(prefers-reduced-motion: reduce)').matches?'auto':'smooth'});
 if(target.suite)pane.focus({preventScroll:true});
}
function renderTerminal(run){
 const jobs=(run?.hosts||[]).flatMap(host=>suiteEntries(host).map(([id,job])=>({key:host.host+'/'+id,host:host.host,job})));
 const selector=$('terminal-job');
 if(document.activeElement!==selector){selector.replaceChildren(new Option(run?.status==='running'?'All running suites':'All recorded suites',''));for(const item of jobs)selector.append(new Option(item.host+' · '+item.job.name+' · '+label(item.job.status),item.key));selector.value=terminalChoice;}
 const active=['running','preparing','stopping'].includes(run?.status)?jobs.filter(item=>['running','preparing','transferring'].includes(item.job.status)):[];
 if(terminalChoice&&!jobs.some(item=>item.key===terminalChoice))terminalChoice='';
 const visible=terminalChoice?jobs.filter(item=>item.key===terminalChoice):(active.length?active:jobs);
 const output=visible.map(({host,job})=>'── '+host+' · '+job.name+' · '+label(job.status)+' · '+(job.phase||'waiting')+' ──\n'+((job.failure_details?.length?'Recorded failure details:\n'+job.failure_details.join('\n')+'\n\nRetained output:\n':'')+((job.logs||[]).join('\n')||(['running','preparing'].includes(run?.status)?'Waiting for process output…':'No process output retained.')))).join('\n\n');
 const pane=$('live-terminal');
 if(pane.textContent!==output){pane.textContent=output||'No test output yet.';if($('follow-output').checked)pane.scrollTop=pane.scrollHeight;}
 $('terminal-status').textContent=(run?.status==='running'?'Live · refreshes every second':'Recorded output')+' · '+active.length+' active suites · Latest 80 lines per suite';
}
$('terminal-job').onchange=()=>{terminalChoice=$('terminal-job').value;renderTerminal(data?.run);};
const el=(tag,text='',className='')=>{const node=document.createElement(tag);node.textContent=text;if(className)node.className=className;return node;};
const label=status=>({reused:'Unchanged · reused',partial:'Partial coverage',stopped:'Stopped',stopping:'Stopping',timed_out:'Timed out',unavailable:'Unavailable',interrupted:'Interrupted',queued:'Queued',transferring:'Transferring',preparing:'Preparing',running:'Running',passed:'Passed',failed:'Failed'}[status]||'Unknown');
const badge=status=>el('span',label(status),'badge '+status);
const seconds=value=>Number.isFinite(value)?value<60?value.toFixed(1)+' s':(value/60).toFixed(1)+' min':'—';
const duration=job=>job.duration_seconds??(job.started&&!terminal.has(job.status)?Math.max(0,((terminal.has(data?.run?.status)?Date.parse(data.run.finished||data.run.updated):Date.now())-Date.parse(job.started))/1000):null);
function coordinatorNode(){
 const node=el('button','','host coordinator-host');node.type='button';node.dataset.terminalHost='compai.local';node.setAttribute('aria-controls','host-terminal');node.setAttribute('aria-pressed',String(selected==='compai.local'));
 node.append(el('strong','compai.local'),el('small','Local machine · Test coordinator'),el('span','Coordinator','badge passed'),el('small','Select for CPUs, containers, and console'));
 node.onclick=()=>{selected='compai.local';render();};return node;
}
function chooseRun(id){chosen=id;selected=null;terminalChoice='';void refresh();}
function renderHistory(){
 const container=$('run-history');const focused=document.activeElement?.closest('.run-history-item')?.dataset.run;container.replaceChildren();
 $('latest-run').disabled=!chosen;
 const records=(data.history||[]).slice(0,10);
 if(!records.length){container.append(el('p','No recorded runs yet.','muted'));return;}
 for(const item of records){
  const button=el('button','','run-history-item '+item.status);button.type='button';button.dataset.run=item.id;
  const active=chosen?chosen===item.id:data.run?.id===item.id;
  button.setAttribute('aria-pressed',String(active));
  const heading=el('span','','run-history-heading');
  const start=Date.parse(item.started),end=item.finished?Date.parse(item.finished):terminal.has(item.status)?NaN:Date.now();
  const elapsed=Number.isFinite(start)&&Number.isFinite(end)?Math.max(0,(end-start)/1000):null;
  heading.append(el('strong',new Date(item.started).toLocaleString()),el('span',seconds(elapsed)+(!terminal.has(item.status)&&elapsed!==null?' elapsed':''),'suite-duration'));
  button.append(heading,badge(item.status),el('code',item.id));
  button.append(el('small',Number.isInteger(item.suites)?item.passed+' / '+item.suites+' jobs passed'+(item.reused?' · '+item.reused+' unchanged':'')+(item.failed?' · '+item.failed+' failed / unavailable':''):'Open recorded results'));
  const work=item.id===data.run?.id?data.run.hosts:historyReports.get(item.id)?.work;
  if(work)button.append(renderRunWork(work));
  const reason=item.id===data.run?.id?summarizeFailure(data.run):historyReports.get(item.id)?.summary;
  if(reason){const preview=el('span',reason.headline.slice(0,200),'run-failure-preview');preview.title=reason.headline;button.append(preview);}
  else button.append(el('span',historyReports.get(item.id)?.unavailable?'Saved explanation unavailable · open run to retry':'Loading saved reason…','run-failure-preview muted'));
  button.onclick=()=>chooseRun(item.id);container.append(button);
 }
 if(focused)for(const button of container.children)if(button.dataset.run===focused)button.focus({preventScroll:true});
}
$('latest-run').onclick=()=>chooseRun('');
function render(){
 const run=data?.run;
 renderOverallProgress(liveData?.run||run);
 document.dispatchEvent(new CustomEvent('test-context',{detail:{run,host:selected}}));
 renderActivity(run);
 const staging=data?.snapshot_staging;
 if(staging)$('snapshot-staging').textContent=(staging.source_sha256?'Snapshot '+staging.source_sha256.slice(0,12)+' · ':'')+staging.status+' · '+Object.entries(staging.workers||{}).map(([host,row])=>host+': '+row.status+(row.phase?' / '+row.phase:'')+(row.error?' / '+row.error:'')).join(' · ');
 renderTerminal(run);
 renderHistory();
 renderFailures($('run-failures'),run,inspectRecordedFailure);
 void loadHistoryReasons();
 $('run').disabled=Boolean(data?.running);$('run').textContent=data?.running?'Tests running…':'Commit, push & test';
 $('stop').disabled=!data?.running||data?.run?.status==='stopping';
 $('stop').textContent=data?.run?.status==='stopping'?'Stopping…':'Stop all tests';
 if(document.activeElement!==$('history')){
  $('history').replaceChildren(new Option('Latest run',''));
  for(const item of (data.history||[]).slice(0,10))$('history').append(new Option(new Date(item.started).toLocaleString()+' · '+label(item.status),item.id));
  $('history').value=chosen;
 }
 if(!run){$('status').textContent='No distributed run yet. Run application tests to test the verified local agents.';$('topology').replaceChildren(coordinatorNode());return;}
 $('status').textContent=label(run.status)+' · Started '+new Date(run.started).toLocaleString()+(run.finished?' · Finished '+new Date(run.finished).toLocaleString():'');
 const image=run.image_progress;
 if(image&&image.phase!=='ready')$('status').textContent+=' · Image '+image.phase+': '+image.image+(image.source?' · '+image.source:'')+(image.destination?' → '+image.destination:'');
 $('error').textContent=run.error||'';
 if(data.running&&run.preparation)$('status').append(' · '+run.preparation);
 const hosts=run.hosts||[];
 const mobiles=hosts.filter(host=>host.kind==='android').length;
 $('command-scope').textContent=mobiles===hosts.length?'Physical Android browser · USB via compai · one test at a time':'Shared file queue · '+(hosts.length-mobiles)+' computer workers'+(mobiles?' · '+mobiles+' physical mobile device':'')+' · coordinator participates';
 const jobs=hosts.flatMap(host=>Object.values(host.jobs||{}));
 const aggregate=summarizeProgress(run);
 $('summary').replaceChildren();
 for(const [name,value]of [['Worker machines',hosts.length],['Fresh passes / reused',jobs.filter(j=>j.status==='passed').length+' / '+jobs.filter(j=>j.status==='reused').length],['Unsuccessful jobs',jobs.filter(j=>['failed','unavailable','timed_out','interrupted'].includes(j.status)).length],['Completed',aggregate.total===null?'Preparing':aggregate.completed+' / '+aggregate.total],['Waiting for capacity',run.pending_tasks?.length||0],['Running jobs',hosts.reduce((n,h)=>n+(h.active_tasks?.length||0),0)],['Repositories pushed',Object.values(run.repositories||{}).filter(repo=>repo.stage==='pushed').length+' / '+Math.max(3,Object.keys(run.repositories||{}).length)]]){const card=el('div','','metric');card.append(el('span',name),el('strong',String(value)));$('summary').append(card);}
 if(selected!=='compai.local'&&!hosts.some(host=>host.host===selected))selected=hosts[0]?.host;
 const focusedHost=document.activeElement?.closest('#topology [data-terminal-host]')?.dataset.terminalHost;
 $('topology').replaceChildren(...(hosts.some(host=>host.local)?[]:[coordinatorNode()]));
 for(const host of hosts){const node=el('button','','host '+host.status);node.type='button';if(host.kind!=='android')node.dataset.terminalHost=host.host;node.setAttribute('aria-pressed',String(host.host===selected));node.append(el('strong',host.name||host.host),el('small',host.kind==='android'?'Physical Android · USB via compai':(host.user||'Worker')+(host.local?' · Local coordinator and worker':'')+' · '+(host.observed_hostname||'Identity not observed yet')),badge(host.status));const jobs=Object.values(host.jobs||{}),finished=jobs.filter(j=>terminal.has(j.status)).length;const progress=el('div','','host-progress'),fill=el('span');fill.style.width=(jobs.length?100*finished/jobs.length:0)+'%';progress.append(fill);if(host.session)node.append(el('small',host.kind==='android'?'1 mobile browser slot · '+host.device?.serial:'Launcher session · '+host.session.status+(host.session.error?' · '+host.session.error:'')));node.append(progress,el('small',finished+' / '+jobs.length+' jobs finished · '+seconds(duration(host))));
 if(host.capacity)node.append(el('small',(host.active_tasks?.length||0)+' active jobs · '+host.capacity.cpus+' CPU budget · '+(host.capacity.memory_mb/1024).toFixed(1)+' GiB memory budget'));
 for(const completed of [false,true]){
 const entries=suiteEntries(host).filter(([,job])=>terminal.has(job.status)===completed);
 if(!entries.length)continue;
 const suites=el('span','','machine-suites');suites.setAttribute('role','list');suites.setAttribute('aria-label',host.host+(completed?' completed suites':' pending suites'));
 if(completed)suites.append(el('span','Completed · '+entries.length,'suite-group-label'));
 for(const [id,job] of entries){
  const tile=el('span','','suite-tile '+job.status);tile.dataset.suite=id;tile.dataset.completed=String(completed);tile.setAttribute('role','listitem');
  const heading=el('span','','suite-card-heading');
  const elapsed=duration(job);
  const timing=job.status==='reused'?'Reused':elapsed===null?'Not started':seconds(elapsed)+(!terminal.has(job.status)?' elapsed':'');
  heading.append(el('strong',job.name),el('span',timing,'suite-duration'));
  tile.append(heading,el('span',label(job.status),'suite-state'));
  if(job.phase_seconds){const phases=Object.entries(job.phase_seconds).map(([phase,value])=>(phase==='prepare'?'Setup':phase==='test'?'Tests':phase)+' '+seconds(value));if(phases.length)tile.append(el('small',phases.join(' · ')));}
  const counts=job.test_counts;
  const detail=[job.phase,counts?`${counts.passed??'—'} passed · ${counts.failed??0} failed`:null].filter(Boolean).join(' · ');
  if(detail)tile.append(el('small',detail));
  tile.title=job.name+' · '+label(job.status)+' · '+timing+(detail?' · '+detail:'');suites.append(tile);
 }
 node.append(suites);}
 node.onclick=()=>{selected=host.host;render();};$('topology').append(node);}
 if(focusedHost)Array.from($('topology').children).find(node=>node.dataset.terminalHost===focusedHost)?.focus({preventScroll:true});
 $('updated').textContent=run.updated?'Last result '+new Date(run.updated).toLocaleTimeString():'';
 const completedEverywhere=id=>hosts.filter(host=>host.jobs?.[id]).every(host=>terminal.has(host.jobs[id].status));
 const ids=[...new Set(hosts.flatMap(host=>host.suites||[]))].sort((a,b)=>Number(completedEverywhere(a))-Number(completedEverywhere(b)));const header=el('tr');header.append(el('th','Test file / check'));for(const host of hosts)header.append(el('th',host.host));$('matrix-head').replaceChildren(header);$('matrix').replaceChildren();
 for(const id of ids){const row=el('tr');row.append(el('th',hosts.find(h=>h.jobs[id])?.jobs[id].name||id));for(const host of hosts){const cell=el('td'),job=host.jobs[id];if(job){cell.className='suite-cell '+job.status;cell.dataset.completed=String(terminal.has(job.status));cell.append(badge(job.status),el('small',(job.phase||'Waiting')+' · '+seconds(duration(job))+(job.test_counts?' · '+(job.test_counts.passed??'—')+' passed / '+(job.test_counts.failed??0)+' failed / '+(job.test_counts.skipped??0)+' skipped':'')));}else cell.append(el('span','Not assigned','muted'));row.append(cell);}$('matrix').append(row);}
 $('timeline').replaceChildren();const first=Date.parse(run.started),end=run.finished?Date.parse(run.finished):Date.now(),span=Math.max(1,(end-first)/1000);
 for(const host of hosts)for(const [,job] of suiteEntries(host)){if(!job.started)continue;const row=el('div','','timeline-row'),track=el('div','','track'),bar=el('span','','bar '+job.status);bar.style.marginLeft=Math.max(0,100*(Date.parse(job.started)-first)/1000/span)+'%';bar.style.width=Math.min(100,100*(duration(job)||0)/span)+'%';track.append(bar);row.append(el('span',host.host+' · '+job.name),track,el('span',seconds(duration(job))));$('timeline').append(row);}
 const host=hosts.find(host=>host.host===selected);$('jobs').replaceChildren();
 if(selected==='compai.local'){$('details-title').textContent='compai.local';$('details-meta').textContent='Local test coordinator · Select a machine to inspect CPU utilization, containers, and console output.';$('jobs').append(el('p','Publishes GitHub commits and collects results from verified local agents.','muted'));}
 if(host){for(const [name,source]of Object.entries(host.source_sync||{}))$('jobs').append(el('p',(name==='.'?'Organization':name)+' · '+source.status+' · '+source.commit,'muted'));$('details-title').textContent=host.name||host.host;$('details-meta').textContent=[host.kind==='android'?'Physical Android browser · USB':host.local?'Local agent':host.transport==='webrtc'?'Authenticated WebRTC agent':'Authenticated LAN agent',host.cpus?host.cpus+' CPUs':null,host.python?'Host Python '+host.python:null,host.error].filter(Boolean).join(' · ');let completedHeading=false;for(const [suiteId,job] of suiteEntries(host)){if(terminal.has(job.status)&&!completedHeading){$('jobs').append(el('h3','Completed jobs','completed-suites-heading'));completedHeading=true;}const details=el('details','','job'),title=el('summary');details.dataset.suite=suiteId;details.open=terminalChoice===host.host+'/'+suiteId;title.append(el('span',job.name+' · '+(job.phase||'Waiting')+' · '+seconds(duration(job))+(job.test_counts?' · '+(job.test_counts.passed??'—')+' passed / '+(job.test_counts.failed??0)+' failed / '+(job.test_counts.skipped??0)+' skipped':'')),badge(job.status));if(!['p2p','mobile'].includes(suiteId)){const retry=el('button','Rerun this job');retry.type='button';retry.onclick=()=>void rerunSelected(host.host,suiteId);details.append(retry);}details.append(title,el('pre',(job.failure_details?.length?'Failure details:\n'+job.failure_details.join('\n')+'\n\nRecent output:\n':'')+(job.logs||[]).join('\n')||'No output recorded yet.'));const visuals=renderVisualArtifacts(job);if(visuals)details.append(visuals);if(job.reused_from){const baseline=el('button','View passing baseline · '+job.reused_from.run_id);baseline.type='button';baseline.onclick=()=>chooseRun(job.reused_from.run_id);details.append(baseline);}$('jobs').append(details);}}
 $('snapshot').textContent=run.source_sha256?'GitHub manifest SHA-256 '+run.source_sha256:'Preparing GitHub commits…';
 $('scope').textContent=(run.scope||'')+' '+Object.entries(run.repositories||{}).map(([name,repo])=>(name==='.'?'Organization':name)+' '+(repo.commit?.slice(0,12)||'pending')+' · '+(repo.stage||'recorded')).join(' · ');
}
async function refresh(){
 if(polling)return;
 polling=true;const requested=chosen;
 try{
  const response=await snapshotFetch('/api/distributed-tests'+(requested?'?id='+encodeURIComponent(requested):''),{cache:'no-store'});
  if(!response.ok)throw Error('Could not load distributed test results ('+response.status+')');
  const result=await response.json();
  let latest=result;
  if(requested){const current=await snapshotFetch('/api/distributed-tests',{cache:'no-store'});if(!current.ok)throw Error('Could not refresh the live run');latest=await current.json();}
  if(requested===chosen){data=requested?{...result,running:latest.running,history:latest.history,active_reruns:latest.active_reruns}:result;liveData=latest;render();}
 }catch(error){if(requested===chosen)$('error').textContent=error.message;}
 finally{polling=false;if(requested!==chosen)void refresh();}
}
$('run').onclick=async()=>{if(data?.running)return;$('run').disabled=true;try{const response=await authorizedAction('/api/distributed-tests/start',{method:'POST'});if(!response.ok)throw Error(await response.text());chosen='';data=await response.json();render();}catch(error){$('error').textContent=error.message;$('run').disabled=false;}};
$('history').onchange=()=>chooseRun($('history').value);
document.addEventListener('visibilitychange',()=>{if(document.visibilityState==='visible')void refresh();});
setInterval(()=>{if(document.visibilityState==='visible')void refresh();},1000);
void refresh();

$('stop').onclick=async()=>{$('stop').disabled=true;try{const response=await authorizedAction('/api/distributed-tests/stop',{method:'POST'});if(!response.ok)throw Error(await response.text());chosen='';data=await response.json();render();}catch(error){$('error').textContent=error.message;void refresh();}};

$('test-commands').append($('run'),$('stop'));

async function rerunSelected(host,suite){
 const args={run_id:data.run.id};if(suite)Object.assign(args,{host,suite});
 try{
  const response=await authorizedAction('/api/distributed-tests/rerun',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(args)});
  if(!response.ok)throw Error('Could not queue rerun ('+response.status+')');
  const result=await response.json();chooseRun(result.run_id);
 }catch(error){$('error').textContent=error.message;}
}
const retryFailed=el('button','Rerun failed suites');retryFailed.type='button';retryFailed.onclick=()=>{if(data?.run)void rerunSelected();};$('latest-run').after(retryFailed);
