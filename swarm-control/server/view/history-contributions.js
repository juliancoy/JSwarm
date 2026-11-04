export function computerContributions(rows){
 const computers=new Map();
 for(const row of rows)for(const machine of row.machines||[]){
  if(!computers.has(machine.name))computers.set(machine.name,{name:machine.name,runs:0,completed_tasks:0,executed_tasks:0,reused_tasks:0,cpu_seconds:null,cpu_measured_tasks:0,estimated_cpu_slot_seconds:null,cpu_estimated_tasks:0});
  const result=computers.get(machine.name);result.runs++;
  for(const key of ['completed_tasks','executed_tasks','reused_tasks','cpu_measured_tasks','cpu_estimated_tasks'])result[key]+=machine[key]||0;
  for(const key of ['cpu_seconds','estimated_cpu_slot_seconds'])if(Number.isFinite(machine[key]))result[key]=(result[key]??0)+machine[key];
 }
 return [...computers.values()];
}
export function rankComputers(rows,key='completed_tasks'){
 return [...rows].sort((a,b)=>(b[key]??-1)-(a[key]??-1)||a.name.localeCompare(b.name));
}
export function duration(seconds){if(!Number.isFinite(seconds))return 'Not recorded';if(seconds>=3600)return (seconds/3600).toFixed(1)+' h';if(seconds>=60)return (seconds/60).toFixed(1)+' min';return seconds.toFixed(1)+' s';}
