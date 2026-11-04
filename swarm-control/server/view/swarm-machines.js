export const stateLabels={running:'Running',failed:'Failed',queued:'Queued',stopped:'Stopped',passed:'Passed',unavailable:'Unavailable',not_run:'Not run',unknown:'Unknown',idle:'No tasks recorded'};
const terminal=new Set(['passed','failed','stopped','cancelled','canceled','interrupted','timed_out']);
export function taskState(value,runStatus){
 let state=({preparing:'running',transferring:'running',timed_out:'failed',cancelled:'stopped',canceled:'stopped',interrupted:'unavailable',reused:'passed',cached:'passed',skipped:'not_run'})[value]||value;
 if(terminal.has(runStatus)&&state==='queued')state='not_run';
 if(terminal.has(runStatus)&&state==='running')state='stopped';
 return stateLabels[state]?state:'unknown';
}
const key=value=>String(value||'').toLowerCase().replace(/\.$/,'').replace(/\.local$/,'');
export function machineCards(data){
 const cards=data.devices.map(device=>({id:device.address||device.hostname,name:device.hostname,address:device.address,reachable:device.reachable,coordinator:device.coordinator,jobs:[],worker:false,state:'idle'}));
 for(const host of data.run.hosts){
  let candidates=cards.filter(card=>host.address&&card.address===host.address);
  if(!candidates.length)candidates=cards.filter(card=>key(card.name)===key(host.host));
  let card=candidates.length===1?candidates[0]:null;
  if(!card){card={id:host.host,name:host.host,address:host.address,jobs:[],reachable:null};cards.push(card)}
  Object.assign(card,{worker:true,cpus:host.cpus,memory_mb:host.memory_mb,workerStatus:host.status,jobs:host.jobs.map(job=>({...job,state:taskState(job.status,data.run.status)}))});
  const states=card.jobs.map(job=>job.state);
  // Empty ready workers are not evidence of passed tasks.
  if(host.status!=='passed')states.push(taskState(host.status,data.run.status));
  card.state=['failed','running','queued','stopped','unavailable','not_run','unknown','passed'].find(state=>states.includes(state))||'idle';
 }
 for(const card of cards){card.counts={};for(const job of card.jobs)card.counts[job.state]=(card.counts[job.state]||0)+1}
 return cards;
}
