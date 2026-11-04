import {summarizeProgress} from './run-progress.js';
const make=(tag,text='',cls='')=>{const e=document.createElement(tag);e.textContent=text;e.className=cls;return e;};
const time=ms=>{const s=Math.max(0,Math.floor(ms/1000));return s<60?s+'s':Math.floor(s/60)+'m '+s%60+'s';};
let observation=null;
export function renderOverallProgress(run){
 const root=document.getElementById('overall-progress');root.replaceChildren();
 if(!run){root.append(make('h2','Overall test progress'),make('p','No run recorded yet.'));return;}
 const p=summarizeProgress(run),now=Date.now();
 const jobs=(run.hosts||[]).flatMap(h=>Object.entries(h.jobs||{}).map(([id,j])=>[h.host,id,j.status,j.phase,j.logs?.at(-1)]));
 const signature=JSON.stringify([run.status,run.preparation,p,run.image_progress?.bytes_completed,run.activity?.at(-1),jobs]);
 if(observation?.id!==run.id)observation={id:run.id,signature,changed:Date.parse(run.activity?.at(-1)?.time)||now};
 else if(signature!==observation.signature)observation={...observation,signature,changed:now};
 const heading=make('div','','panel-heading');
 const title=make('div');title.append(make('div',p.terminal?'RECORDED RUN':'LIVE · ALL MACHINES','eyebrow'),make('h2','Overall test progress'));
 heading.append(title,make('strong',p.total===null?'Preparing':p.completed+' / '+p.total+' completed'+(p.percent===null?'':' · '+p.percent+'%'),'overall-total'));root.append(heading);
 const progress=make('progress');progress.setAttribute('aria-label','Overall completed test jobs');progress.max=p.total||1;progress.style.accentColor=p.failed?'#f09a86':p.unavailable||p.stopped?'#e5bb77':'#83d9ae';if(p.total!==null)progress.value=p.completed;root.append(progress);
 const metrics=make('div','','overall-counts');
 for(const [text,value,status] of [['Passed',p.passed,'passed'],['Reused',p.reused,'reused'],['Failed',p.failed,'failed'],['Running / preparing',p.running,'running'],[p.terminal?'Not run':'Queued',p.queued,'queued'],['Unavailable',p.unavailable,'unavailable'],['Stopped',p.stopped,'stopped']]){
  if(status==='stopped'&&!value)continue;const e=make('span','',status);e.append(make('strong',p.total===null?'—':String(value)),document.createTextNode(' '+text));metrics.append(e);
 }root.append(metrics);
 const elapsed=(Date.parse(run.finished)||now)-(Date.parse(run.started)||now),quiet=now-observation.changed;
 root.append(make('p',(run.status||'unknown').replaceAll('_',' ')+' · '+time(elapsed)+' elapsed · '+p.ready+' / '+p.nodes+' nodes ready'+(p.excluded?' · '+p.excluded+' excluded':'')+' · Run '+run.id,'muted'));
 root.append(make('p',p.terminal?'Results saved.':(run.preparation||'Jobs are assigned across available machines.')+' · Last observed progress '+time(quiet)+' ago'+(quiet>=90000?' · No new progress observed; check worker output below.':''),'overall-stage'));
 root.append(make('small',p.total===null?'The job total becomes available after source and worker preparation.':'Counts cover test-file and build jobs across all machines; retry attempts are counted once.'+(run.force?' Forced run: cached passes are bypassed.':' Matching recorded passes may be reused.'),'muted'));
 const output=make('a','View live output');output.href='#live-terminal';root.append(output);
 if(run.gpu){const coverage=run.gpu.coverage||[];root.append(make('p','CUDA kernel checks: '+run.gpu.status+' · '+coverage.filter(h=>h.eligible).length+' eligible workers'+coverage.filter(h=>h.reason==='NVIDIA runtime missing').map(h=>' · '+h.host+': NVIDIA runtime missing').join(''),'muted'));}
}
