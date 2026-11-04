import {summarizeFailure} from './failure-summary.js';
const el=(tag,text,cls)=>{const node=document.createElement(tag);if(text!==undefined)node.textContent=text;if(cls)node.className=cls;return node;};
const expanded=new Map(),rendered=new WeakMap();
export function renderFailures(root,run,onInspect){
 const revision=JSON.stringify([run?.id,run?.status,run?.updated,run?.error]);
 if(rendered.get(root)===revision)return;rendered.set(root,revision);
 root.replaceChildren();
 const summary=summarizeFailure(run);
 root.append(el('h3',['running','preparing','stopping'].includes(run?.status)?'Failure evidence so far':'Why this run ended'));
 if(!run){root.append(el('p',summary.headline,'muted'));return;}
 const context=el('div',undefined,'failure-run-context');
 context.append(el('strong',run.id),el('span',run.status+' · '+new Date(run.started).toLocaleString()));
 if(run.source_sha256)context.append(el('code','Snapshot '+run.source_sha256.slice(0,16)));
 for(const [repo,value]of Object.entries(run.repositories||{}))if(value.commit)context.append(el('span',(repo==='.'?'Organization':repo)+' '+value.commit.slice(0,12)+(value.dirty?' + working changes':'')));
 root.append(context);
 const totals=summary.counts;
 root.append(el('p',[
  (totals.suiteFailures||0)+' suite failures',
  (totals.timeouts||0)+' timeouts',
  (totals.unavailable||0)+' unavailable',
  (totals.interrupted||0)+' interrupted',
  (totals.stopped||0)+' stopped',
  summary.blocked+' blocked by infrastructure',
  summary.notExecuted+' not executed'
 ].join(' · '),'failure-counts'));
 if(run.image_progress){const image=run.image_progress;root.append(el('p','Last recorded image operation: '+image.phase+' · '+image.image+([image.source,image.destination].filter(Boolean).length?' · '+[image.source,image.destination].filter(Boolean).join(' → '):''),'muted'));}
 if(!summary.incidents.length){root.append(el('p',summary.headline,'muted'));return;}
 for(const [index,item]of summary.incidents.entries()){
  const key=run.id+'|'+item.key,card=el('details',undefined,'failure-incident');card.open=expanded.has(key)?expanded.get(key):index===0;
  const heading=el('summary');heading.append(el('span',item.kind,'failure-category'),el('strong',item.reason));card.append(heading);
  const meta=['Phase: '+item.phase,item.evidenceSource];if(Number.isInteger(item.exitCode))meta.push('Exit '+item.exitCode);
  card.append(el('p',meta.join(' · '),'muted'));
  if(item.coordinator&&summary.blocked)card.append(el('p',summary.blocked+' suites inherited an infrastructure failure status. This is not evidence that those tests failed.','failure-impact'));
  const targets=el('div',undefined,'failure-targets');
  for(const target of item.affected){
   const text=target.host+(target.name?' · '+target.name:'')+(Number.isInteger(target.exitCode)?' · exit '+target.exitCode:'');
   const button=el('button',text);button.type='button';button.title=target.suite?'Show recorded suite output':'Inspect recorded machine details';button.onclick=()=>onInspect(target);targets.append(button);
  }
  if(targets.childNodes.length)card.append(targets);
  card.append(el('pre',item.evidence.length?item.evidence.join('\n'):'No error output was retained.','failure-excerpt'));
  card.addEventListener('toggle',()=>{if(card.isConnected){expanded.set(key,card.open);}});root.append(card);
 }
 root.append(el('p','Reasons above quote saved evidence. Repeated matching errors are grouped. Logs are bounded; missing output means the cause is unknown. These are historical observations, not the current state of a machine.','muted failure-disclaimer'));
}
