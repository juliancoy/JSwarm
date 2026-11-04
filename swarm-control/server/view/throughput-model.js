import {valid} from './levels-model.js';
import {processLabel,attributionLabel} from './resource-context.js';
export function throughputSources(data,direction='download',device='',now=Date.now(),group='sender',scope='devices') {
 const capture=data.link_traffic,age=now-Date.parse(capture?.updated),fresh=capture&&!capture.capture_error&&Number.isFinite(age)&&age>=-5000&&age<10000;
 if(!fresh||!capture.flows)return {fresh:false,rows:[],sources:[],total:null,allTotal:null};
 const devices=new Map((data.devices||[]).map(d=>[d.mac?.toLowerCase(),d]));
 const rows=[];let allTotal=0;
 const ledger=scope==='uplink'?capture.uplink_flows:capture.flows;
 if(!ledger)return {fresh:false,rows:[],sources:[],total:null,allTotal:null};
 for(const f of ledger.records||[]){
  const rx=valid(f.download_mbps)?f.download_mbps:0,tx=valid(f.upload_mbps)?f.upload_mbps:0;
  const rate=direction==='download'?rx:direction==='upload'?tx:rx+tx;
  allTotal+=rate;
  if(device&&f.device_mac!==device||rate<=0)continue;
  const d=devices.get(f.device_mac?.toLowerCase()),meta=data.remote_ips?.[f.remote_ip]||{};
  rows.push({...f,rate,deviceName:f.device_mac==='uplink-unattributed'?'Unattributed uplink':d?.hostname||d?.ip||f.local_ip,remoteName:meta.ptr||f.remote_ip,provider:meta.organization||meta.isp||'Provider unknown',asn:meta.asn});
 }
 rows.sort((a,b)=>b.rate-a.rate);
 const total=rows.reduce((sum,r)=>sum+r.rate,0),grouped=new Map();
 for(const r of rows){
  let key=direction==='download'?r.remote_ip:r.device_mac,name=direction==='download'?r.remoteName:r.deviceName,detail=direction==='download'?r.provider:r.local_ip;
  if(group==='process'){
   const owners=r.recent_processes||[];
   key=owners.length?'process:'+owners.map(p=>p.pid+':'+(p.start_ticks||'unknown')).sort().join('+'):'unattributed:'+r.device_mac;
   name=owners.length?owners.map(p=>processLabel(p,data)).join(' / '):r.deviceName+' · process unknown';
   detail=attributionLabel(r);
  }
  if(!grouped.has(key))grouped.set(key,{id:key,name,detail,rate:0,flows:0});const g=grouped.get(key);if(group==='process'&&g.detail!==detail)g.detail='Mixed live / retained owner evidence; inspect individual flows';g.rate+=r.rate;g.flows++;
 }
 const sources=[...grouped.values()].sort((a,b)=>b.rate-a.rate).map(r=>({...r,share:total?r.rate/total:0}));
 return {fresh:true,rows,sources,total,allTotal,sampleSeconds:capture.sample_seconds,updated:capture.updated,evictions:capture.flows.evictions||0};
}
