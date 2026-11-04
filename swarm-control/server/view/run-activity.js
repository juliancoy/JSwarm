import {summarizeProgress} from './run-progress.js';
const done=new Set(['passed','partial','failed','stopped','interrupted','unavailable','timed_out','reused']);
const make=(tag,text,cls='')=>{const node=document.createElement(tag);node.textContent=text;node.className=cls;return node;};
const age=seconds=>seconds<60?Math.floor(seconds)+'s':seconds<3600?Math.floor(seconds/60)+'m '+Math.floor(seconds%60)+'s':Math.floor(seconds/3600)+'h '+Math.floor(seconds%3600/60)+'m';
let rendered='';
export function renderActivity(run){
 const root=document.getElementById('run-activity');
 if(!run){root.replaceChildren(make('h2','Run activity'),make('p','No run selected.','muted'));rendered='';return;}
 const jobs=(run.hosts||[]).flatMap(h=>Object.values(h.jobs||{}));
 const aggregate=summarizeProgress(run),finished=aggregate.completed;
 const active=(run.hosts||[]).flatMap(h=>Object.values(h.jobs||{}).filter(j=>['running','preparing'].includes(j.status)).map(j=>h.host+' · '+j.name+(j.phase?' · '+j.phase:'')));
 const complete=done.has(run.status);
 const image=run.image_progress;
 const imageText=image?({pulling:'Downloading image',downloaded:'Download completed',building:'Building image',publishing:'Publishing image',distributing_layers:'Transferring image over LAN',cached:'Image cached',ready:'Image ready'}[image.phase]||image.phase.replaceAll('_',' '))+': '+image.image+[image.source,image.destination].filter(Boolean).map(v=>' · '+v).join(''):'';
 let transferEta='';
 if(image?.phase==='distributing_layers'){
  const fresh=Number.isFinite(image.updated)&&Date.now()/1000-image.updated<15;
  const eta=fresh&&Number.isFinite(image.eta_seconds)?'~'+age(Math.ceil(image.eta_seconds)):'estimating…';
  const bytes=v=>(v/1048576).toFixed(1)+' MiB';
  transferEta=' · Transfer ETA '+eta;
  if(fresh&&image.bytes_total>0)transferEta+=' · '+bytes(image.bytes_completed)+' / '+bytes(image.bytes_total)+' known layers'+(image.bytes_per_second>0?' · '+bytes(image.bytes_per_second)+'/s':'');
  if(fresh&&image.transfer_phase==='finalizing')transferEta=' · Finalizing layers; finish time unavailable';
 }
 const preparing=(run.hosts||[]).filter(h=>h.status==='preparing');
 const stage=complete?'Run '+run.status:run.status==='stopping'?'Stopping workers':run.status==='preparing'?(image&& !['ready','cached'].includes(image.phase)?imageText:run.preparation||'Preparing workers and images'):active.length?active.length+' jobs active'+(preparing.length?' · '+preparing.length+' nodes preparing':''):preparing.length?'Preparing '+preparing.length+' nodes independently':'Waiting for worker results';
 // Keep the event list stable between polls so readers retain their scroll position.
 if(root.dataset.run!==run.id){root.dataset.run=run.id;root.replaceChildren();rendered='';
  const heading=make('div','','panel-heading');heading.append(make('h2','Run activity'),make('span','', 'activity-clock'));root.append(heading,make('strong','','activity-stage'),make('p','','activity-detail'));
  const progress=document.createElement('progress');progress.setAttribute('aria-label','Completed jobs');root.append(progress,make('p','','activity-workers'),make('p','','post-deployment-status'));
  const list=make('ol','','activity-events');list.setAttribute('aria-label','Recorded run events, newest first');root.append(list,make('small','Latest 300 coordinator events are retained with each run. Process output is below.','muted'));
 }
 root.querySelector('.activity-stage').textContent=stage+(!complete?transferEta:'');
 const elapsed=Math.max(0,((Date.parse(run.finished)||Date.now())-Date.parse(run.started))/1000);
 root.querySelector('.activity-clock').textContent=age(elapsed)+' elapsed';
 const last=run.activity?.at(-1)?.time||run.updated||run.started;
 const quiet=Math.max(0,(Date.now()-Date.parse(last))/1000);
 const total=aggregate.total;
 root.querySelector('.activity-detail').textContent=(total===null?'Preparing the job plan':finished+' / '+total+' jobs completed')+(complete?'':' · Last coordinator event '+age(quiet)+' ago')+(!complete&&quiet>=60?' · Waiting for an update; this alone does not mean the operation has stalled.':'');
 const progress=root.querySelector('progress');progress.max=total||1;if(total===null)progress.removeAttribute('value');else progress.value=finished;
 root.querySelector('.activity-workers').textContent=[...active,...preparing.map(h=>h.host+' · '+(h.preparation||'preparing'))].join('   /   ')||(complete?'Results saved.':'Each node starts independently when its images are ready.');
 const post=run.post_deployment;
 root.querySelector('.post-deployment-status').textContent=post?'Post-deployment: '+post.status.replaceAll('_',' ')+' · '+(post.checks||[]).map(c=>c.name+': '+c.status+' — '+c.detail).join(' / '):complete?'Post-deployment checks run after a passing test run and wait for its deployed commit.':'';
 if(run.pending_tasks?.length)root.querySelector('.activity-workers').append(' · '+run.pending_tasks.length+' jobs waiting for capacity');
 const events=run.activity||[];
 root.querySelector('small.muted').textContent=events.length?'Latest 300 coordinator events retained with this run. Process output is below.':'Current snapshot only. Full event recording begins with the next run.';
 const fallback=events.length?events:[{time:run.updated||run.started,message:imageText||run.preparation||'Run '+run.status}];
 const stamp=JSON.stringify(fallback);
 if(stamp!==rendered){rendered=stamp;const list=root.querySelector('ol');const position=list.scrollTop;list.replaceChildren(...fallback.slice(-300).reverse().map(event=>{const row=make('li','','activity-event '+(event.level||'info'));const time=make('time',new Date(event.time).toLocaleTimeString());time.dateTime=event.time;time.title=new Date(event.time).toLocaleString();row.append(time,make('span',[event.host,event.message,event.source?'From '+event.source:null].filter(Boolean).join(' · ')));return row;}));list.scrollTop=position;}
}
