export function statusClass(status){
 if(['completed','passed','reused','verified'].includes(status))return 'status-completed';
 if(['failed','error','conflict','timed_out','interrupted','blocked'].includes(status))return 'status-failed';
 if(status==='partial')return 'status-partial';
 if(['running','active','preparing','transferring','starting','streaming','stopping'].includes(status))return 'status-running';
 if(['queued','pending','waiting','blocked','planning','not scheduled','waiting_capacity','waiting_inputs','waiting_dependency','retrying','claimed'].includes(status))return 'status-queued';
 return 'status-unknown';
}
const success=c=>statusClass(c.status)==='status-completed';
function bytes(n){return n>=1e9?(n/1e9).toFixed(2)+' GB':n>=1e6?(n/1e6).toFixed(1)+' MB':Math.round(n)+' B';}
export function progressText(row){
 const total=row.total_units,done=row.completed_units;
 if(!Number.isFinite(total)||total<=0)return Number.isFinite(done)?`${done} ${row.unit||'units'} completed · total not yet known`:'';
 let text=done==null?'Read position unavailable':row.unit==='input bytes'?`${bytes(done)} / ${bytes(total)}`:`${done} / ${total} ${row.unit||'units'}`;
 if(Number.isFinite(row.units_per_second)&&row.units_per_second>0)text+=' · '+(['input bytes','bytes'].includes(row.unit)?bytes(row.units_per_second):row.units_per_second.toFixed(2)+' '+(row.unit||'units'))+'/s';
 if(Number.isFinite(row.read_eta_seconds)&&row.read_eta_seconds>0)text+=' · ~'+Math.ceil(row.read_eta_seconds/60)+' min to finish reading';
 return text;
}
export function groupRows(children){
 if(!children.some(c=>c.group))return children;
 const grouped=new Map();for(const c of children){const name=c.group||'Other';if(!grouped.has(name))grouped.set(name,[]);grouped.get(name).push(c);}
 return [...grouped].map(([name,rows])=>{
  const measured=rows.every(c=>Number.isFinite(c.total_units)&&c.total_units>0&&c.unit===rows[0].unit);
  const total=measured?rows.reduce((n,c)=>n+c.total_units,0):null;
  const done=measured?rows.reduce((n,c)=>n+(success(c)?c.total_units:(c.completed_units||0)),0):null;
  return {name,children:rows,status:rows.every(success)?'completed':rows.some(c=>statusClass(c.status)==='status-running')?'running':rows.some(c=>statusClass(c.status)==='status-failed')?'failed':'queued',
   percent:total?Math.round(1000*done/total)/10:Math.round(100*rows.filter(success).length/rows.length),total_units:total,completed_units:done,unit:rows[0].unit,
   detail:`${rows.filter(success).length}/${rows.length} verified complete`+(measured?' · input scan progress':' · work-item completion'),worker:[...new Set(rows.map(c=>c.worker))].join(', ')};
 });
}

// Parallel workers finish when the slowest unfinished input finishes reading.
export function readEta(row, observedAt){
 if(success(row))return {text:'Verified complete',seconds:0};
 if(row.stale||row.runtime_stale)return {text:'ETA unavailable · stale observation'};
 if(statusClass(row.status)==='status-queued'&&row.waiting_label)return {text:row.waiting_label};
 if(['failed','error','conflict','timed_out','interrupted'].includes(row.status)&&!row.children?.length)return {text:'ETA unavailable · failed: '+(row.detail||row.name||'worker error')};
 if(row.status==='partial')return {text:row.input_blockers?.length?'Partial results · waiting for required inputs':'Partial results · full scope incomplete'};
 if(row.status==='waiting_inputs')return {text:'Waiting for required inputs'};
 if(row.status==='preparing')return {text:'Preparing workers · execution has not started'};
 if(row.eta_unavailable_reason||row.progress?.eta_unavailable_reason)return {text:'ETA unavailable · '+(row.eta_unavailable_reason||row.progress.eta_unavailable_reason)};
 if(Number.isFinite(row.execution_eta_seconds)&&row.execution_eta_seconds>=0&&statusClass(row.status)==='status-running')return {...etaLabel(row.execution_eta_seconds),text:etaLabel(row.execution_eta_seconds).text.replace('Read ETA',(row.eta_scope||'Execution')+' ETA')+' · rough observed rate'};
 if(row.children?.length){
  const pending=row.children.filter(c=>!success(c));
  if(!pending.length)return {text:'Awaiting verification'};
  const estimates=pending.map(c=>readEta(c,observedAt));
  if(estimates.every(e=>Number.isFinite(e.seconds))){const eta=etaLabel(Math.max(...estimates.map(e=>e.seconds)));if(pending.some(c=>Number.isFinite(c.execution_eta_seconds)))eta.text=eta.text.replace('Read ETA','Stage ETA')+' · rough observed rate';return eta;}
  const failed=pending.find(c=>['failed','error','conflict','timed_out','interrupted'].includes(c.status));
  if(failed)return {text:'ETA unavailable · failed step: '+failed.name};
  const waiting=pending.find(c=>['queued','pending','waiting','blocked','planning','not scheduled','waiting_capacity','waiting_inputs','waiting_dependency','retrying','claimed','preparing'].includes(c.status));
  if(waiting)return {text:'ETA unavailable · '+(waiting.status==='not scheduled'?'unscheduled step: ':'waiting for ')+waiting.name};
  return {text:'ETA unavailable · '+estimates.find(e=>!Number.isFinite(e.seconds)).text.replace(/^ETA unavailable · /,'')};
 }
 if(row.status==='preparing')return {text:'Preparing workers · execution has not started'};
 if(row.status==='not scheduled')return {text:'Not scheduled'+(row.waiting_for?.length?' · requires '+row.waiting_for.join(', '):'')};
 if(row.status==='blocked')return {text:'Blocked · '+(row.detail||'prerequisites pending')};
 if(statusClass(row.status)!=='status-running')return {text:'ETA unavailable · '+(statusClass(row.status)==='status-queued'?'waiting to start':'awaiting measurements')};
 const p=row.progress||row;
 if(p.sampled_at&&observedAt-p.sampled_at>=90)return {text:'ETA unavailable · stale observation'};
 if(Number.isFinite(p.total_units)&&p.total_units>0&&Number.isFinite(p.completed_units)&&p.completed_units>=p.total_units)return etaLabel(0);
 let seconds=p.read_eta_seconds;
 if(Number.isFinite(p.completed_units)&&!(p.total_units>0))return {text:'ETA unavailable · total work not yet known'};
 const remaining=p.total_units-p.completed_units;
 if(!Number.isFinite(seconds)&&Number.isFinite(remaining)&&p.completed_units!=null){
  if(p.units_per_second>0)seconds=Math.max(0,remaining)/p.units_per_second;
  else if(row.attempt_started>0&&observedAt>row.attempt_started&&p.completed_units>0)
   seconds=Math.max(0,remaining)*(observedAt-row.attempt_started)/p.completed_units;
 }
 return Number.isFinite(seconds)&&seconds>=0?etaLabel(seconds):{text:'ETA unavailable · awaiting measurements'};
}
function etaLabel(seconds){
 const minutes=Math.ceil(seconds/60);
 const duration=minutes<60?Math.max(1,minutes)+' min':Math.floor(minutes/60)+' h '+minutes%60+' min';
 return {seconds,text:seconds===0?'Reading finished · awaiting verification':'Read ETA ~'+duration};
}
