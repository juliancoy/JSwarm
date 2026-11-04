// Count logical jobs once across the fleet, including retries and unassigned work.
const active=new Set(['running','preparing','transferring','starting','stopping']);
const terminal=new Set(['passed','reused','failed','timed_out','unavailable','interrupted','partial','stopped']);
export function summarizeProgress(run={}){
 const catalog=Array.isArray(run.task_catalog)?run.task_catalog:Object.keys(run.task_catalog||{});
 const planned=new Set(catalog),pending=new Set(run.pending_tasks||[]),groups=new Map();
 for(const id of new Set([...catalog,...pending]))groups.set(id,[]);
 for(const host of run.hosts||[])for(const [id,job] of Object.entries(host.jobs||{})){
  const key=planned.has(id)||pending.has(id)?id:host.host+'/'+id;
  if(!groups.has(key))groups.set(key,[]);groups.get(key).push(job);
 }
 const counts={passed:0,reused:0,failed:0,unavailable:0,stopped:0,running:0,queued:0};
 for(const [key,jobs] of groups){
  let state;
  if(jobs.some(j=>j.status==='passed'))state='passed';
  else if(jobs.some(j=>j.status==='reused'))state='reused';
  else if(jobs.some(j=>active.has(j.status)))state='running';
  else if(pending.has(key)||!jobs.length||jobs.some(j=>j.status==='queued'))state='queued';
  else{
   const last=[...jobs].sort((a,b)=>(Date.parse(b.finished||b.updated||b.started)||0)-(Date.parse(a.finished||a.updated||a.started)||0))[0]?.status;
   state=['failed','timed_out'].includes(last)?'failed':last==='stopped'?'stopped':'unavailable';
  }
  counts[state]++;
 }
 const known=!!catalog.length||run.granular===true||!['queued','preparing'].includes(run.status);
 const total=known?groups.size:null;
 const completed=counts.passed+counts.reused+counts.failed+counts.unavailable+counts.stopped;
 const hosts=run.hosts||[];
 return {...counts,total,completed:known?completed:0,percent:total?Math.floor(100*completed/total):null,
  terminal:terminal.has(run.status),ready:hosts.filter(h=>h.status!=='unavailable'&&(h.session?.verified||['running','passed'].includes(h.status))).length,
  nodes:hosts.length,excluded:hosts.filter(h=>h.status==='unavailable').length};
}
