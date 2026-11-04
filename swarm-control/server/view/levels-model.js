import {hostCpuDetail,hostCpuContext,cpuStatusLabel} from './cpu-report.js';
export const valid=v=>typeof v==='number'&&Number.isFinite(v)&&v>=0;
export function deviceLevels(data,metrics,mode,now=Date.now()) {
 const hosts=metrics.hosts||[],used=new Set(),rows=[];
 if(mode==='disk')return hosts.flatMap(host=>(host.disks||[]).flatMap(disk=>['read','write'].map(direction=>{
  const fresh=disk.status==='live'&&now/1000-disk.sampled_at<15,id=host.host+':disk:'+disk.identity;
  return {id:id+':'+direction,deviceId:id,name:host.host+' · '+disk.name+' · '+direction,machine:host.host,disk:disk.name,model:disk.model,direction,
   sampleTime:disk.sampled_at*1000,value:fresh&&valid(disk[direction+'_percent'])?disk[direction+'_percent']:null,throughput:fresh&&valid(disk[direction+'_bytes_per_second'])?disk[direction+'_bytes_per_second']/1e6:null,
   unavailableLabel:fresh?'Observing…':'No data',linkLabel:disk.capacity_basis?`Observed sustained: ${(disk.capacity_bytes_per_second/1e6).toFixed(1)} MB/s`:'Observing sustained capacity (30 s under load)',
   basis:'Read and write each use the same observed sustained combined-throughput baseline. Workload-dependent estimate, not a hardware limit; bursts can exceed 100%.'};
 })));
 if(['gpu','vram'].includes(mode))return hosts.flatMap(h=>(h.hardware?.gpus||[]).map((gpu,index)=>{
  const sampledAt=h.hardware.sampled_at??h.sampled_at;
  const fresh=now/1000-sampledAt<15&&(h.hardware.sampled_at!==undefined||h.status==='live'&&!h.hardware.stale);
  let value=mode==='gpu'?gpu.utilization_percent:valid(gpu.memory_total_mib)&&gpu.memory_total_mib>0&&valid(gpu.memory_free_mib)?100*(1-gpu.memory_free_mib/gpu.memory_total_mib):null;
  if(!fresh||!valid(value))value=null;
  return {id:h.host+':gpu:'+(gpu.pci_id||index),name:h.host+' · '+gpu.name,sampleTime:sampledAt*1000,value,basis:mode==='gpu'?(gpu.utilization_basis||'Live GPU utilization; unavailable backends remain unmeasured'):'GPU memory used, including reserved UMA where reported; unavailable counters remain unmeasured'};
 }));

 if(['network','send','receive'].includes(mode))for(const host of hosts){
  used.add(host.host);
  if(!host.network_interfaces?.length)continue;
  for(const iface of host.network_interfaces){
   const field=mode==='send'?'tx_mbps':mode==='receive'?'rx_mbps':'network_mbps';
   const recent=now/1000-iface.sampled_at<15,disconnected=recent&&(iface.status==='disconnected'||['down','lowerlayerdown','notpresent'].includes(iface.state));
   const fresh=recent&&iface.status==='live'&&!disconnected;
   rows.push({id:`${host.host}:interface:${iface.ifindex}:${iface.name}`,name:`${host.host} · ${iface.name}`,machine:host.host,interface:iface.name,linkLabel:networkLinkLabel(iface,recent,disconnected),
    utilization:fresh&&valid(iface.link_speed_mbps)&&iface.link_speed_mbps>0&&['send','receive'].includes(mode)&&valid(iface[field])?100*iface[field]/iface.link_speed_mbps:null,
    healthLabel:networkHealthLabel(iface,recent),health:recent?iface:null,
    sampleTime:iface.sampled_at*1000,value:fresh&&valid(iface[field])?iface[field]:null,throughput:fresh&&valid(iface.network_mbps)?iface.network_mbps:null,disconnected,unavailableLabel:disconnected?'Not connected':'No data',basis:`Physical interface ${iface.name} · ${iface.state||'unknown state'} · ${iface.window_seconds??'unknown'}s counter interval`});
  }
 }
 const field={network:'network_mbps',disk:'disk_busy_percent',ram:'ram_percent',cpu:'percent',send:'network_tx_mbps',receive:'network_rx_mbps'}[mode];
 const hostValue=h=>h?.status==='live'&&now/1000-h.sampled_at<15&&valid(h[field])?h[field]:null;
 const cpuInfo=h=>mode==='cpu'?{basis:h?hostCpuDetail(h)+' · '+hostCpuContext(h):'No host telemetry',unavailableLabel:h?cpuStatusLabel(now/1000-h.sampled_at>=15?'stale':h.status):'No telemetry'}:{};
 const capture=data.link_traffic, fresh=capture&&!capture.capture_error&&now-Date.parse(capture.updated)<10000;
 for(const d of data.devices||[]){
  const h=hosts.find(h=>Boolean(d.hostname)&&(h.host===d.hostname||h.observed_hostname===d.hostname.replace(/\.local$/,''))||h.addresses?.includes(d.ip)||d.self&&h.host==='compai.local');if(h&&used.has(h.host))continue;if(h)used.add(h.host);
  let value=hostValue(h),basis='Host telemetry';
  if(mode==='network'&&value===null&&!d.self){const p=fresh?capture.peers?.[d.mac?.toLowerCase()]:null;value=p&&valid(p.rx_mbps)&&valid(p.tx_mbps)?p.rx_mbps+p.tx_mbps:null;basis='Gateway-observed Ethernet traffic';}
  if(['send','receive'].includes(mode)&&value===null){
   const p=d.self?data.interface_traffic?.[data.interface]:fresh?capture.peers?.[d.mac?.toLowerCase()]:null;
   const available=d.self?p&&now-Date.parse(p.updated)<10000:!!p;
   const field=mode==='send'?(d.self?'tx_mbps':'rx_mbps'):(d.self?'rx_mbps':'tx_mbps');
   value=available&&valid(p[field])?p[field]:null;
   basis=d.self?'Coordinator LAN interface only':'Gateway-observed device traffic; switched traffic may be missing';
  }
  if(d.hostname_source==='cached_mdns')basis+=' · Name from the last mDNS observation; connectivity is measured separately';
  if(['network','send','receive'].includes(mode)&&value===null&&!data.link_traffic?.capture_error)continue;
  rows.push({id:d.mac||d.ip,deviceId:['julian-latitude.local','julian-minipc.local'].includes(h?.host||d.hostname)?(h?.host||d.hostname):undefined,name:(d.hostname||d.ip)+(['network','send','receive'].includes(mode)?(h?' · interface unavailable':' · gateway only'):''),value,basis,telemetryHost:h?.host,sampleTime:['cpu','ram'].includes(mode)?h?.sampled_at*1000:undefined,...cpuInfo(h)});
 }
 for(const h of hosts)if(!used.has(h.host)&&!['network','send','receive'].includes(mode))rows.push({id:h.host,name:h.host+(['network','send','receive'].includes(mode)?' · interface unavailable':''),telemetryHost:h.host,sampleTime:h.sampled_at*1000,value:hostValue(h),basis:'Host telemetry',...cpuInfo(h)});
 return rows;
}
export function deviceColor(id){const host=String(id).split(':')[0].split('#')[0].toLowerCase();if(host==='julian-latitude.local'||host==='julian-latitude')return '#ef8cce';if(host==='julian-minipc.local'||host==='julian-minipc')return '#ad91ff';let hash=0;for(const c of id)hash=(hash*31+c.charCodeAt(0))|0;return `hsl(${(hash>>>0)%360} 78% 68%)`;}

export function compareNetworkRows(a,b){
 return (b.throughput??b.value??-1)-(a.throughput??a.value??-1)||(a.machine||a.name).localeCompare(b.machine||b.name)||a.name.localeCompare(b.name);
}

export function networkLinkLabel(iface,recent=true,disconnected=false){
 if(!recent)return 'Link speed unavailable';
 if(disconnected)return 'Link: Not connected';
 const rate=value=>value>=1000?`${Number((value/1000).toFixed(2))} Gb/s`:`${Number(value.toFixed(1))} Mb/s`;
 if(valid(iface.link_speed_mbps)&&iface.link_speed_mbps>0)return `Link: ${rate(iface.link_speed_mbps)}${['full','half'].includes(iface.link_duplex)?' · '+iface.link_duplex+' duplex':''}`;
 const wifi=['rx','tx'].filter(direction=>valid(iface['link_'+direction+'_mbps'])&&iface['link_'+direction+'_mbps']>0).map(direction=>direction.toUpperCase()+' '+rate(iface['link_'+direction+'_mbps']));
 return wifi.length?'Wi-Fi PHY: '+wifi.join(' · '):'Link speed unavailable';
}

export function networkHealthLabel(iface,recent=true){
 if(!recent)return 'Health data stale';
 const fields=['rx_errors_delta','tx_errors_delta','rx_dropped_delta','tx_dropped_delta'];
 if(!fields.every(k=>valid(iface[k])))return 'Health counters warming or unavailable';
 const errors=iface.rx_errors_delta+iface.tx_errors_delta,drops=iface.rx_dropped_delta+iface.tx_dropped_delta;
 return `${errors} new errors · ${drops} new drops`+(valid(iface.carrier_changes_delta)&&iface.carrier_changes_delta?` · ${iface.carrier_changes_delta} link changes`:'');
}

export function taskTransferLabel(rows,now=Date.now()/1000){
 const fresh=rows.filter(r=>now-r.sampled_at>=0&&now-r.sampled_at<15&&r.status==='running'&&valid(r.bytes_per_second));
 if(fresh.length)return `Task payload: ${(fresh.reduce((n,r)=>n+r.bytes_per_second,0)/1e6).toFixed(1)} MB/s · ${fresh.length} incoming transfer${fresh.length===1?'':'s'}`;
 const latest=rows.filter(r=>now-r.sampled_at>=0&&now-r.sampled_at<900).sort((a,b)=>b.sampled_at-a.sampled_at)[0];
 if(latest&&['stale','failed','interrupted'].includes(latest.status))return latest.status==='stale'?'Task-transfer telemetry stale':latest.status==='failed'?'Last task transfer failed validation':'Last task transfer interrupted';
 const last=rows.filter(r=>r.status==='complete'&&now-r.sampled_at>=0&&now-r.sampled_at<900&&valid(r.average_bytes_per_second)).sort((a,b)=>b.sampled_at-a.sampled_at)[0];
 return last?`${last.task_id==='network-transfer-validation'?'Transfer check':'Last task transfer'}: ${(last.average_bytes_per_second/1e6).toFixed(1)} MB/s average · completed ${Math.floor((now-last.sampled_at)/60)} min ago`:'No task-transfer data';
}

export function mergeNetworkMeasurements(metrics,resources,now=Date.now()/1000){
 const supplemental=new Map((resources?.hosts||[]).map(h=>[h.host,h]));
 return {...metrics,hosts:(metrics.hosts||[]).map(host=>{
  const extra=supplemental.get(host.host);if(!extra)return host;
  const fresh=(extra.network_interfaces||[]).filter(r=>now-r.sampled_at>=0&&now-r.sampled_at<15&&r.status!=='warming');
  return fresh.length?{...host,network_interfaces:fresh,task_transfers:extra.task_transfers||[]}:host;
 })};
}
