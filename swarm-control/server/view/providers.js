const families=[
 ['google','Google',[15169,19527],['google.com','googleapis.com','googleusercontent.com','gstatic.com','1e100.net','youtube.com','ytimg.com','googlevideo.com','googletagmanager.com','doubleclick.net']],
 ['cloudflare','Cloudflare',[13335,209242],['cloudflare.com','cloudflareinsights.com','cloudflare.net','cdnjs.cloudflare.com']],
 ['facebook','Facebook / Meta',[32934],['facebook.com','facebook.net','fbcdn.net','instagram.com','cdninstagram.com','whatsapp.com','whatsapp.net','meta.com']],
 ['apple','Apple',[714,6185],['apple.com','icloud.com','aaplimg.com']],
 ['microsoft','Microsoft',[8075,8068],['microsoft.com','microsoftonline.com','azure.com','live.com','office.com']],
 ['amazon','Amazon / AWS',[16509,14618],['amazon.com','amazonaws.com','cloudfront.net']],
 ['canonical','Canonical',[41231],['canonical.com','ubuntu.com']]
];
export function provider(info={},hostname='') {
 const hosts=[hostname,info.ptr,info.network_domain].filter(Boolean).map(host=>host.toLowerCase().replace(/\.$/,''));
 for(const [id,name,asns,domains] of families)if(asns.includes(Number(info.asn))||hosts.some(host=>domains.some(domain=>host===domain||host.endsWith('.'+domain))))return {id:'provider:'+id,name};
 if(info.network_domain)return {id:'provider:domain:'+info.network_domain.toLowerCase(),name:info.organization||info.network_domain};
 if(info.asn)return {id:'provider:asn:'+info.asn,name:info.organization||'AS'+info.asn};
 return null;
}
export function groupNetwork(view) {
 const groups=new Map(),ids=new Map();
 for(const node of view.nodes){
  const family=!node.mac&&!node.gap ? provider(node.remote_info):null;
  const id=family?.id||node.id;ids.set(node.id,id);
  if(!family){groups.set(id,node);continue;}
  const group=groups.get(id)||{id,title:family.name,external:true,context:true,provider_group:true,members:[],ports:[],addresses:[],traffic_summary:{pairs:0,bytes:0,mbps:0},visibility:node.visibility};
  group.members.push(node);group.addresses.push(node.ip);group.perspective ||=node.perspective;group.you ||=node.you;
  group.distance=Math.min(group.distance??Infinity,node.distance??Infinity);
  for(const key of ['pairs','bytes','mbps'])group.traffic_summary[key]+=(node.traffic_summary?.[key]||0);
  groups.set(id,group);
 }
 const graph=new Map();
 for(const edge of view.edges){
  const a=ids.get(edge.a),b=ids.get(edge.b);if(a===b)continue;
  const pair=[a,b].sort().join('|'),aggregate=groups.get(a)?.provider_group||groups.get(b)?.provider_group;
  if(!aggregate){graph.set(pair,edge);continue;}
  const item=graph.get(pair)||{a,b,kind:'provider-relationship',capacity_mbps:null,actual_mbps:null,bytes:0,members:[],relationships:[],evidence_sources:[],capacity_basis:'Provider aggregate; no single physical-link capacity',traffic_basis:'Sum of distinct observed outbound endpoint relationships at this gateway'};
  item.members.push(edge);
  for(const flow of [edge,...edge.relationships||[]])if(['outbound','observed-connection'].includes(flow.kind)){
   item.relationships.push({...flow,a:ids.get(flow.a),b:ids.get(flow.b)});
   if(flow.kind==='observed-connection')item.traffic_basis='Sum of observed bidirectional endpoint IP throughput; not a physical link';
   if(!item.observed||Date.parse(flow.observed)>Date.parse(item.observed))item.observed=flow.observed;
   if(Number.isFinite(flow.actual_mbps))item.actual_mbps=(item.actual_mbps||0)+flow.actual_mbps;
   item.bytes+=flow.bytes||0;
  }
  item.evidence_sources=[...new Set([...item.evidence_sources,...edge.evidence_sources||[],edge.evidence].filter(Boolean))];
  graph.set(pair,item);
 }
 const nodes=[...groups.values()],id=ids.get(view.id)||view.id;
 return {...view,id,nodes,edges:[...graph.values()],source:nodes.find(n=>n.id===id)};
}
export function providerTitle(id) {return families.find(f=>'provider:'+f[0]===id)?.[1]||id;}
export function groupWebsiteGraph(graph) {
 const groups=new Map(),ids=new Map();
 for(const node of graph.nodes){
  let hostname='';try{hostname=new URL(node.origin).hostname;}catch{}
  // Website origins group by their explicit domain family, not by a shared hosting IP.
  const family=node.id===graph.root ? null : provider({},hostname),id=family?.id||node.id;
  ids.set(node.id,id);
  if(!family){groups.set(id,node);continue;}
  const group=groups.get(id)||{id,origin:id,members:[],provider_group:true,requests:0,bytes:0,unknown_bytes:0,errors:0,blocked:0,cancelled:0,auth_challenges:0,statuses:{},types:{},ips:[]};
  group.members.push(node);
  for(const key of ['requests','bytes','unknown_bytes','errors','blocked','cancelled','auth_challenges'])group[key]+=node[key]||0;
  for(const key of ['statuses','types'])for(const [name,count] of Object.entries(node[key]||{}))group[key][name]=(group[key][name]||0)+count;
  group.ips=[...new Set([...group.ips,...node.ips])];groups.set(id,group);
 }
 const edges=new Map();
 for(const edge of graph.edges){const a=ids.get(edge.a),b=ids.get(edge.b);if(a===b)continue;
  const key=[a,b].sort().join('|'),item=edges.get(key)||{a,b,requests:0,context:true,evidence:[]};
  item.requests+=edge.requests;item.context&&=edge.context;item.evidence.push(...edge.evidence);edges.set(key,item);
 }
 return {...graph,nodes:[...groups.values()],edges:[...edges.values()]};
}
