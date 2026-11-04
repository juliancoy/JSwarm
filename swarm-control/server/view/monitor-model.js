export const numeric=value=>typeof value==='number'&&Number.isFinite(value)&&value>=0;
export function currentPerformance(host,now=Date.now()/1000){
 const p=host?.performance;
 return p&&p.schema_version===1&&!p.stale&&numeric(p.sampled_at)&&now-p.sampled_at>=-5&&now-p.sampled_at<15?p:null;
}
export function memorySeries(host,now=Date.now()/1000){
 const p=currentPerformance(host,now),m=p?.memory;
 return [
  {key:'dram_read_bytes_per_second',title:'DRAM read',unit:'B/s',value:m?.dram_read_bytes_per_second??null},
  {key:'dram_write_bytes_per_second',title:'DRAM write',unit:'B/s',value:m?.dram_write_bytes_per_second??null},
  {key:'cache references',title:'Cache references',unit:'events/s',value:m?.cache?.['cache references']??null},
  {key:'cache misses',title:'Cache misses',unit:'events/s',value:m?.cache?.['cache misses']??null},
  {key:'mbm_total_bytes',title:'Root-group MBM total',unit:'B/s · LLC ↔ memory',value:m?.mbm?.mbm_total_bytes??null},
  {key:'mbm_local_bytes',title:'Root-group MBM local',unit:'B/s · overlaps total',value:m?.mbm?.mbm_local_bytes??null},
 ].map(row=>({...row,value:numeric(row.value)?row.value:null,sampledAt:p?.sampled_at??null}));
}
export function sortedRows(rows,index,reverse){
 const known=rows.filter(row=>row[index]!==null&&row[index]!==undefined);
 known.sort((a,b)=>{const x=a[index],y=b[index];return (typeof x==='number'&&typeof y==='number'?x-y:String(x).localeCompare(String(y)))*(reverse?-1:1);});
 return [...known,...rows.filter(row=>row[index]===null||row[index]===undefined)];
}

export const SWARM='__swarm__';
export function aggregateSwarm(hosts,now=Date.now()/1000){
 const unique=new Map();for(const host of hosts||[]){const key=host.machine||host.host;if(!key)continue;const previous=unique.get(key);if(!previous||host.sampled_at>previous.sampled_at)unique.set(key,host);}
 const all=[...unique.values()],fresh=h=>h.status==='live'&&numeric(h.sampled_at)&&now-h.sampled_at>=-5&&now-h.sampled_at<15;
 const live=all.filter(fresh),samples=live.map(h=>({h,p:currentPerformance(h,now)})),performances=samples.filter(s=>s.p);
 const sum=values=>{const known=values.filter(numeric);return known.length?known.reduce((a,b)=>a+b,0):null;};
 const maximum=values=>{const known=values.filter(numeric);return known.length?Math.max(...known):null;};
 const weighted=key=>{const rows=live.filter(h=>numeric(h[key])&&numeric(h.cpus)&&h.cpus>0);const cores=sum(rows.map(h=>h.cpus));return cores?rows.reduce((n,h)=>n+h[key]*h.cpus,0)/cores:null;};
 const mem=samples.map(({h,p})=>p?.memory||{total_bytes:h.ram_total_bytes,available_bytes:numeric(h.ram_total_bytes)&&numeric(h.ram_used_bytes)?h.ram_total_bytes-h.ram_used_bytes:null});
 const memory={};for(const key of ['total_bytes','available_bytes','cached_bytes','dirty_bytes','writeback_bytes','dram_read_bytes_per_second','dram_write_bytes_per_second'])memory[key]=sum(mem.map(m=>m[key]));
 memory.used_bytes=sum(mem.map(m=>numeric(m.total_bytes)&&numeric(m.available_bytes)&&m.available_bytes<=m.total_bytes?m.total_bytes-m.available_bytes:null));
 memory.pressure_avg10={some:maximum(mem.map(m=>m.pressure_avg10?.some)),full:maximum(mem.map(m=>m.pressure_avg10?.full))};
 memory.cache=Object.fromEntries(['cache references','cache misses'].map(key=>[key,sum(mem.map(m=>m.cache?.[key]))]));
 memory.mbm=Object.fromEntries(['mbm_total_bytes','mbm_local_bytes'].map(key=>[key,sum(mem.map(m=>m.mbm?.[key]))]));
 memory.counters=performances.flatMap(({h,p})=>(p.memory?.counters||[]).map(r=>({...r,name:h.host+' · '+r.name})));
 const annotated=(key,field)=>performances.flatMap(({h,p})=>(p[key]||[]).map(r=>({...r,node:h.host,[field]:h.host+' · '+r[field]})));
 const filesystems=performances.flatMap(({h,p})=>{const seen=new Set();return (p.filesystems||[]).filter(r=>{const key=JSON.stringify([r.device,r.type,r.total_bytes]);if(seen.has(key))return false;seen.add(key);return true;}).map(r=>({...r,node:h.host,device:h.host+' · '+r.device,mount:h.host+' · '+r.mount}));});
 const disks=live.flatMap(h=>(h.disks||[]).filter(r=>r.status==='live'&&now-r.sampled_at>=-5&&now-r.sampled_at<15).map(r=>({...r,node:h.host,name:h.host+' · '+r.name})));
 const interfaces=live.flatMap(h=>(h.network_interfaces||[]).filter(r=>r.status==='live'&&now-r.sampled_at>=-5&&now-r.sampled_at<15).map(r=>({...r,node:h.host,name:h.host+' · '+r.name})));
 const storage=filesystems.filter(r=>['ext4','ext3','ext2','xfs','btrfs','ntfs','ntfs3','fuseblk','zfs'].includes(r.type));
 const gpus=live.filter(h=>h.hardware&&!h.hardware.stale&&now-(h.hardware.sampled_at??h.sampled_at)<15).flatMap(h=>(h.hardware.gpus||[]).map(r=>({...r,node:h.host,name:h.host+' · '+r.name})));
 const times=live.map(h=>h.sampled_at),sampled=times.length?Math.min(...times):now;
 const scope=`Entire swarm · ${live.length}/${all.length} nodes reporting fresh telemetry; ${performances.length}/${all.length} performance collectors. Totals include measured values only; missing/stale values are excluded, not zero. CPU percentages are weighted by logical cores; memory pressure is the highest node value. Network RX and TX are separate: intra-swarm traffic appears at both endpoints. Disk totals describe physical device I/O; filesystem aliases are counted once per node.`;
 const p={schema_version:1,sampled_at:sampled,scope,memory,processes:annotated('processes','name'),filesystems,
  connections:annotated('connections','local'),lan:annotated('lan','interface'),gpu:{rates:{},clients:performances.flatMap(({h,p})=>(p.gpu?.clients||[]).map(r=>({...r,node:h.host,device:h.host+' · '+r.device})))},
  notes:all.filter(h=>!fresh(h)).map(h=>h.host+': unavailable or stale'),limits:{reporting_nodes:live.length,total_nodes:all.length}};
 return {host:SWARM,aggregate:true,status:live.length?'live':'unavailable',sampled_at:sampled,performance:live.length?p:null,
  percent:weighted('percent'),cpu_iowait_percent:weighted('cpu_iowait_percent'),cpu_steal_percent:weighted('cpu_steal_percent'),cpus:sum(live.map(h=>h.cpus)),
  logical_cpus:live.flatMap(h=>(h.logical_cpus||[]).map(r=>({...r,id:h.host+' · '+r.id}))),
  hardware:{sampled_at:sampled,gpus},disks,network_interfaces:interfaces,
  totals:{disk_total:sum(storage.map(r=>r.total_bytes)),disk_available:sum(storage.map(r=>r.available_bytes)),disk_used:sum(storage.map(r=>numeric(r.total_bytes)&&numeric(r.available_bytes)&&r.available_bytes<=r.total_bytes?r.total_bytes-r.available_bytes:null)),disk_read:sum(disks.map(r=>r.read_bytes_per_second)),disk_write:sum(disks.map(r=>r.write_bytes_per_second)),network_rx:sum(interfaces.map(r=>r.rx_mbps)),network_tx:sum(interfaces.map(r=>r.tx_mbps)),
   gpu_busy:sum(gpus.map(r=>numeric(r.utilization_percent)?r.utilization_percent/100:null)),gpu_measured:gpus.filter(r=>numeric(r.utilization_percent)).length,
   vram_used:sum(gpus.map(r=>numeric(r.memory_total_mib)&&numeric(r.memory_free_mib)&&r.memory_free_mib<=r.memory_total_mib?(r.memory_total_mib-r.memory_free_mib)*2**20:null)),vram_total:sum(gpus.map(r=>numeric(r.memory_total_mib)?r.memory_total_mib*2**20:null)),vram_free:sum(gpus.map(r=>numeric(r.memory_free_mib)?r.memory_free_mib*2**20:null))},scope};
}
