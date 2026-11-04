import {edgeFlows} from './edge-throughput.js';
import {resourceProcess,attributionLabel} from './resource-context.js';
const valid=n=>typeof n==='number'&&Number.isFinite(n)&&n>=0;
export function downloadArtefacts(edge,nodes,data,coordinator,now=Date.now()){
 const match=edgeFlows(edge,nodes,data,now),groups=new Map();
 for(const flow of match.rows){
  if(!valid(flow.download_mbps)||flow.download_mbps<=0)continue;
  const owners=(flow.recent_processes||[]).map(p=>({...p}));
  // A shared socket remains one group; never duplicate its measured rate per owner.
  const key=owners.length?owners.map(p=>p.pid+':'+(p.start_ticks||'unknown')).sort().join('+'):flow.device_mac+'|'+flow.remote_ip;
  const item=groups.get(key)||{rate:0,owners,files:[],databases:[],remote:flow.remote_ip,ownership:attributionLabel(flow)};
  if(item.ownership!==attributionLabel(flow))item.ownership='Mixed live / retained owner evidence';
  item.rate+=flow.download_mbps;
  for(const owner of owners){
   const process=resourceProcess(owner,data,now);
   if(process){
    owner.script=owner.script||process.script;
    for(const f of process.files||[])if(f.writable&&!item.files.some(p=>p.path===f.path))item.files.push(f);
    for(const db of process.database_endpoints||[])if(!item.databases.some(d=>d.address===db.address&&d.port===db.port))item.databases.push(db);
   }
  }
  groups.set(key,item);
 }
 const operations=[];
 const dockerOwner=match.rows.some(f=>(f.recent_processes||[]).some(p=>/^(dockerd|containerd|buildkitd)$/.test(p.name)));
 const aggregateHost=match.hostContext&&!match.exact;
 if(dockerOwner||aggregateHost){
  const feed=data.docker_downloads;
  if(feed?.status==='available'&&now/1000-feed.sampled_at<15)for(const p of feed.active||[])operations.push({image:p.image,evidence:'Concurrent host pull command; this edge’s bytes are not assigned to the image'});
  const progress=coordinator?.run?.image_progress;
  if(coordinator&&now-coordinator.received<8000&&coordinator.run?.status==='running'&&['pulling','distributing'].includes(progress?.phase)){
   const localHost=(data.devices||[]).find(d=>d.self)?.hostname;
   if(progress.source===localHost||progress.destination===localHost)operations.unshift({image:progress.image,evidence:progress.phase==='pulling'?'Coordinator registry pull · host context, not an exact connection match':'Coordinator LAN image transfer · host context'});
  }
 }
 const unique=[...new Map(operations.map(p=>[p.image,p])).values()].slice(0,3);
 return {groups:[...groups.values()].sort((a,b)=>b.rate-a.rate).slice(0,3),operations:unique,scope:match.scope,exact:match.exact};
}
