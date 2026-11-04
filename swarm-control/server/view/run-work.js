// A compact record of actual assignments, including reused and unexecuted work.
export function renderRunWork(hosts){
 const container=document.createElement('span');container.className='run-work';
 let total=0;
 for(const host of hosts||[]){
  const jobs=Object.entries(host.jobs||{});if(!jobs.length)continue;
  const group=document.createElement('span');group.className='run-work-machine';
  const machine=document.createElement('strong');machine.textContent=host.host;group.append(machine);
  for(const [id,job] of jobs){
   total++;
   const row=document.createElement('span');row.className='run-work-suite';
   const name=document.createElement('span');name.textContent=job.name||id;
   const result=document.createElement('span');result.className='run-work-result';
   const status=job.not_executed?'Not executed':job.status==='reused'?'Reused previous result':String(job.status||'Unknown').replaceAll('_',' ');
   const counts=job.test_counts;
   const tests=counts?Object.entries(counts).filter(([,n])=>Number.isFinite(n)).map(([key,n])=>n+' '+key).join(', '):'';
   const duration=Number.isFinite(job.duration_seconds)?(job.duration_seconds<60?job.duration_seconds.toFixed(1)+' s':(job.duration_seconds/60).toFixed(1)+' min'):'';
   result.textContent=[status,tests,duration].filter(Boolean).join(' · ');
   row.append(name,result);group.append(row);
  }
  container.append(group);
 }
 if(!total)container.textContent='No suite assignments recorded.';
 return container;
}
