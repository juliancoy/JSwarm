const valid=n=>typeof n==='number'&&Number.isFinite(n)&&n>=0;
export const edgeID=edge=>[edge.a,edge.b].sort().join('|')+'|'+edge.kind;
const fresh=(stamp,now)=>{const age=now-Date.parse(stamp);return Number.isFinite(age)&&age>=-5000&&age<10000;};
const ips=node=>new Set([node?.ip,...node?.addresses||[],...(node?.members||[]).flatMap(n=>[n.ip,...n.addresses||[]])].filter(Boolean));
export function edgeFlows(edge,nodes,data,now=Date.now()){
 const capture=data.link_traffic;
 if(!capture?.flows||capture.capture_error||!fresh(capture.updated,now))return {rows:[],scope:'No fresh flow attribution',exact:false,hostContext:false};
 const byID=new Map(nodes.map(n=>[n.id,n])),a=byID.get(edge.a),b=byID.get(edge.b),self=data.devices?.find(d=>d.self);
 if(!a||!b)return {rows:[],scope:'Endpoints unavailable',exact:false,hostContext:false};
 let predicate=null,exact=false,hostContext=false,scope='No path-specific artefact attribution';
 const local=a.mac?a:b.mac?b:null,remote=a.external?a:b.external?b:null;
 if(local&&remote){
  const addresses=ips(remote);predicate=f=>f.device_mac?.toLowerCase()===local.mac.toLowerCase()&&addresses.has(f.remote_ip);exact=true;
  scope='Matched device and remote endpoint IPs';hostContext=local.self===true;
 }else if(['wireless','lan-attachment','ethernet'].includes(edge.kind)){
  const client=[a,b].find(n=>n.mac&&n.id!==data.router?.mac&&!n.self);
  if(client){predicate=f=>f.device_mac?.toLowerCase()===client.mac.toLowerCase()&&f.interface===data.interface;scope='Device Internet flows seen at gateway; physical attachment path is approximate';}
  else if([a,b].some(n=>n.self)){predicate=f=>f.interface===data.interface;hostContext=true;scope='Gateway interface context; excludes switched LAN traffic';}
 }else if(edge.kind==='route'&&[a,b].some(n=>n.self)&&[a,b].some(n=>n.ip===data.upstream?.ip)){
  predicate=f=>f.device_mac===self?.mac&&f.interface===data.upstream?.interface;hostContext=true;scope='Host flows on the upstream interface; transit attribution is not measured';
 }
 return {rows:predicate?(capture.flows.records||[]).filter(predicate):[],scope,exact,hostContext};
}
export function edgeMeasurement(edge,nodes,data,now=Date.now()){
 const match=edgeFlows(edge,nodes,data,now);
 if(match.exact&&match.rows.length){
  return {value:match.rows.reduce((sum,f)=>sum+(valid(f.download_mbps)?f.download_mbps:0)+(valid(f.upload_mbps)?f.upload_mbps:0),0),
   stamp:data.link_traffic.updated,basis:'Observed bidirectional IP throughput · receive + send'};
 }
 return {value:valid(edge.actual_mbps)&&fresh(edge.observed,now)?edge.actual_mbps:null,stamp:edge.observed,
  basis:edge.kind==='outbound'||edge.kind==='provider-relationship'?'Observed outbound throughput only':edge.traffic_basis||'No throughput measurement'};
}
export class EdgeHistory{
 constructor(){this.records=new Map();}
 update(edges,nodes,data,now=Date.now()){
  for(const edge of edges){
   const sample=edgeMeasurement(edge,nodes,data,now),key=edgeID(edge),metric=sample.basis;
   let record=this.records.get(key);
   if(!record||record.basis!==metric)record={points:[],basis:metric};
   record.seen=now;
   const stamp=sample.value===null?Math.floor(now/1000)*1000:Date.parse(sample.stamp);
   const last=record.points.at(-1);
   if(!last||stamp>last.time)record.points.push({time:stamp,value:sample.value});
   record.points=record.points.filter(p=>p.time>=now-120000).slice(-120);
   record.current=sample.value;record.peak=Math.max(0,...record.points.map(p=>p.value||0));
   this.records.set(key,record);
  }
  for(const [key,record]of this.records)if(now-record.seen>120000)this.records.delete(key);
  while(this.records.size>1024)this.records.delete(this.records.keys().next().value);
 }
 get(edge){return this.records.get(edgeID(edge));}
}
export function edgeChart(edge,record,start,end,radiusA=0,radiusB=0,now=Date.now()){
 if(!record)return null;
 // Keep time orientation stable when an edge is viewed from its other endpoint.
 const reverse=edge.a>edge.b,p=reverse?end:start,q=reverse?start:end;
 const r1=reverse?radiusB:radiusA,r2=reverse?radiusA:radiusB;
 const dx=q.x-p.x,dy=q.y-p.y,length=Math.hypot(dx,dy),trimA=r1+12,trimB=r2+12;
 if(length-trimA-trimB<28)return null;
 const ux=dx/length,uy=dy/length,nx=uy,ny=-ux,available=length-trimA-trimB;
 const known=record.points.filter(p=>p.time>=now-120000);
 const span=Math.min(120000,Math.max(10000,now-(known[0]?.time??now)));
 const recentPeak=known.some(p=>valid(p.value))?Math.max(0,...known.map(p=>p.value||0)):null;
 const peak=Math.max(.001,recentPeak),height=Math.min(22,Math.max(10,available*.12));
 const segments=[];let segment=[],previous=null;
 const flush=()=>{if(segment.length)segments.push(segment);segment=[];};
 // Bound drawing work independently of frame rate and edge length.
 const stride=Math.max(1,Math.ceil(known.length/60));
 for(let i=0;i<known.length;i++){
  const point=known[i];if(point.value===null||previous!==null&&point.time-previous>10000)flush();
  previous=point.time;
  if(point.value===null||i%stride&&i!==known.length-1)continue;
  const t=Math.max(0,Math.min(1,(point.time-now+span)/span));
  const along=trimA+t*available,offset=height*point.value/peak;
  segment.push({x:p.x+ux*along+nx*offset,y:p.y+uy*along+ny*offset});
 }
 flush();
 return {edge,segments,peak:recentPeak,basis:record.basis,current:now-record.seen<10000&&fresh(record.points.at(-1)?.time?new Date(record.points.at(-1).time).toISOString():null,now)?record.current:null,
  baseline:[{x:p.x+ux*trimA,y:p.y+uy*trimA},{x:q.x-ux*trimB,y:q.y-uy*trimB}]};
}
export function distanceToSegment(point,a,b){
 const dx=b.x-a.x,dy=b.y-a.y,length=dx*dx+dy*dy;
 const t=length?Math.max(0,Math.min(1,((point.x-a.x)*dx+(point.y-a.y)*dy)/length)):0;
 return Math.hypot(point.x-a.x-t*dx,point.y-a.y-t*dy);
}
export function hitEdge(point,charts){
 let closest=12,result=null;
 for(const chart of charts){
  const paths=[chart.baseline,...chart.segments];
  for(const path of paths){
   if(path.length===1){const distance=Math.hypot(point.x-path[0].x,point.y-path[0].y);if(distance<closest){closest=distance;result=chart;}}
   for(let i=1;i<path.length;i++){const distance=distanceToSegment(point,path[i-1],path[i]);if(distance<closest){closest=distance;result=chart;}}
  }
 }
 return result;
}
