import {statusClass} from './task-display.js';

// Execution ownership stays with the controller; this is only an assignment index.
export function machineKey(value){
 const name=value.trim().toLowerCase().replace(/\.$/,'').replace(/\.local$/,'');
 return ['unassigned','waiting for worker','awaiting worker'].includes(name)?'Unassigned':name;
}

export function machineJobs(tasks){
 const machines=new Map();
 function visit(row,task,inherited){
  const worker=row.worker&&row.worker!=='unassigned'?row.worker:inherited;
  if(row.children?.length){for(const child of row.children)visit(child,task,worker);return;}
  const names=[...new Set((worker||'').split(',').map(machineKey).filter(s=>s&&s!=='Unassigned'))];
  if(!names.length)names.push('Unassigned');
  for(const name of names){
   if(!machines.has(name))machines.set(name,[]);
   machines.get(name).push({...row,workflow:task.name,shared:names.length>1,key:task.id+':'+row.id});
  }
 }
 for(const task of tasks)visit(task,task,null);
 return [...machines].sort(([a],[b])=>a==='Unassigned'?1:b==='Unassigned'?-1:a.localeCompare(b)).map(([name,jobs])=>({name,jobs:jobs.sort((a,b)=>Number(statusClass(a.status)==='status-completed')-Number(statusClass(b.status)==='status-completed'))}));
}
