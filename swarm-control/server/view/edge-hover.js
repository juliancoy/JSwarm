import {downloadArtefacts} from './edge-artefacts.js';
import {label} from './model.js';
const el=(tag,text,cls)=>{const n=document.createElement(tag);if(text!==undefined)n.textContent=text;if(cls)n.className=cls;return n;};
const rate=value=>value===null?'Unknown':value<1?(value*1000).toFixed(1)+' kb/s':value.toFixed(2)+' Mb/s';
export function createEdgeHover(scene){
 const tip=el('div',undefined,'edge-hover');tip.id='edge-hover';tip.role='tooltip';tip.hidden=true;scene.append(tip);
 return {
  hide(){tip.hidden=true;},
  show(chart,point,nodes,data,coordinator){
   const a=nodes.find(n=>n.id===chart.edge.a),b=nodes.find(n=>n.id===chart.edge.b);if(!a||!b){tip.hidden=true;return;}
   const context=downloadArtefacts(chart.edge,nodes,data,coordinator);
   tip.replaceChildren(el('strong',label(a)+' ↔ '+label(b),'edge-hover-title'));
   const numbers=el('div',undefined,'edge-hover-numbers');numbers.append(el('span',rate(chart.current)+' now'),el('span',rate(chart.peak)+' recent peak'));tip.append(numbers);
   tip.append(el('p',chart.basis+'. Last two minutes, as observed in this tab. Each edge auto-scales independently; time runs along the edge.','edge-hover-scope'));
   tip.append(el('h4','Current download context'));
   for(const item of context.groups.slice(0,2)){
    const group=el('div',undefined,'edge-hover-artefact');
    const name=item.owners.length?item.owners.map(p=>(p.script?p.script.split('/').pop()+' · ':'')+p.name+' (PID '+p.pid+')').join(' / '):item.remote;
    group.append(el('strong',name),el('small',rate(item.rate)+' observed download · '+(context.exact?'matched endpoints':'device/interface context')));
    group.append(el('small',item.ownership));
    const files=[...item.files].sort((a,b)=>Number(Boolean(b.database_hint))-Number(Boolean(a.database_hint))||Number(/^\.download/.test(b.filename))-Number(/^\.download/.test(a.filename)));
    if(files.length){group.append(el('span','Open receiver files (context): '+files.slice(0,2).map(f=>f.filename).join(', ')));}
    else group.append(el('span','Payload filename unknown.'));
    if(item.databases.length){const db=item.databases[0];group.append(el('span','Related DB endpoint (port hint): '+db.engine_hint+' · '+db.address+':'+db.port+' · database name unknown'));}
    tip.append(group);
   }
   for(const operation of context.operations.slice(0,2)){const group=el('div',undefined,'edge-hover-artefact');group.append(el('strong',operation.image),el('small',operation.evidence));tip.append(group);}
   if(!context.groups.length&&!context.operations.length)tip.append(el('p','No current artefact attribution. Encrypted traffic does not expose filenames.','edge-hover-scope'));
   tip.append(el('p',context.scope+'. Open files and concurrent image pulls do not prove a socket-to-file mapping.','edge-hover-scope'));
   tip.hidden=false;
   const box=scene.getBoundingClientRect();tip.style.maxHeight=Math.max(120,box.height-24)+'px';
   tip.style.left=Math.max(8,Math.min(point.x+18,box.width-tip.offsetWidth-8))+'px';
   tip.style.top=Math.max(8,Math.min(point.y+18,box.height-tip.offsetHeight-8))+'px';
  }
 };
}
