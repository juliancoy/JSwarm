import {providerTitle} from './providers.js';
export const originName = origin => {if(origin.startsWith('provider:'))return providerTitle(origin);try{return new URL(origin).host;}catch{return origin;}};
export function changes(current, previous) {
  if(!previous)return null;
  const now=new Set(current.origins.map(n=>n.origin)),before=new Set(previous.origins.map(n=>n.origin));
  return {added:[...now].filter(n=>!before.has(n)),absent:[...before].filter(n=>!now.has(n)),requests:current.requests-previous.requests,bytes:current.bytes-previous.bytes};
}
export function graph(run) {
  const root=new URL(run.site).origin;
  const nodes=new Map(run.summary.origins.map(node=>[node.origin,{...node,id:node.origin}]));
  if(!nodes.has(root))nodes.set(root,{id:root,origin:root,requests:0,bytes:0,errors:0,blocked:0,unknown_bytes:0,ips:[],statuses:{},types:{}});
  const edges=new Map();
  for(const item of run.summary.links){
    if(!nodes.has(item.source)||!nodes.has(item.target)||item.source===item.target)continue;
    const key=[item.source,item.target].sort().join('|');
    const edge=edges.get(key)||{a:item.source,b:item.target,requests:0,context:false,evidence:[]};
    edge.requests+=item.requests;edge.evidence.push(item);edges.set(key,edge);
  }
  for(const node of nodes.values())if(node.id!==root && ![...edges.values()].some(edge=>edge.a===node.id||edge.b===node.id)){
    edges.set([root,node.id].sort().join('|'),{a:root,b:node.id,requests:node.requests,context:true,evidence:[]});
  }
  return {root,nodes:[...nodes.values()],edges:[...edges.values()]};
}
