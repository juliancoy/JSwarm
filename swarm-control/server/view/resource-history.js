// Restore coordinator samples without inventing historical gateway measurements.
export function recordResourceHistory(histories,data,metrics,now,rowsFor){
 const frames=(metrics.history||[]).filter(frame=>Number.isFinite(frame.sampled_at)&&frame.sampled_at*1000>=now-300000&&frame.sampled_at*1000<=now);
 const historicalNetwork={devices:data.devices||[]};
 for(const metric of Object.keys(histories)){
  const history=histories[metric];
  for(const frame of [...frames,{sampled_at:now/1000,hosts:metrics.hosts||[],current:true}]){
   const time=frame.sampled_at*1000;
   if(!frame.current&&!frame.hosts?.length&&!['network','split'].includes(metric))continue;
   for(const row of rowsFor(frame.current?data:frame.network||historicalNetwork,{hosts:frame.hosts},metric,time)){
    if(frame.partial&&['cpu','ram'].includes(metric)&&!row.telemetryHost)continue;
    // Older frames never saved gateway counters. Their absence must not
    // overwrite actual live readings with alternating artificial gaps.
    if(!frame.current&&!frame.network&&['network','split'].includes(metric)&&row.value===null)continue;
    if(!history.has(row.id))history.set(row.id,[]);
    const points=history.get(row.id);
    const measuredTime=Number.isFinite(row.sampleTime)&&row.value!==null?row.sampleTime:time;
    // Repeated reads of the same server frame must not duplicate points.
    const existing=points.findIndex(point=>point.time===measuredTime);
    if(existing>=0)points[existing]={time:measuredTime,value:row.value};
    else points.push({time:measuredTime,value:row.value});
   }
  }
  for(const [id,points] of history){
   const bounded=points.filter(point=>point.time>=now-300000).sort((a,b)=>a.time-b.time).slice(-2048);
   if(bounded.length)history.set(id,bounded);else history.delete(id);
  }
 }
}
