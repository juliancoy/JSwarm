const el=(tag,text,cls)=>{const n=document.createElement(tag);if(text!==undefined)n.textContent=text;if(cls)n.className=cls;return n;};
const opened=new Set();
export function resourceProcess(owner,data,now=Date.now()){
 const context=data.process_resources;
 if(context?.status!=='live'||now/1000-context.sampled_at>=10)return null;
 const process=context.processes?.[String(owner.pid)]||null;
 if(process&&owner.start_ticks!==undefined&&owner.start_ticks!==process.start_ticks)return null;
 if(process&&owner.script&&process.script&&owner.script!==process.script)return null;
 return process;
}
export function processLabel(owner,data){
 const r=resourceProcess(owner,data),script=(owner.script||r?.script)?.split('/').pop();
 return (script?script+' · ':'')+owner.name+' (PID '+owner.pid+')';
}
export function attributionLabel(flow){
 return ({current:'Live socket-owner snapshot',retained:'Retained packet-time owner match',ambiguous:'Ambiguous socket ownership',unknown:'No socket-owner match'})[flow.process_attribution]||'Socket ownership freshness unavailable';
}
export function renderResources(root,data,flowRows,device){
 root.replaceChildren();
 const self=data.devices?.find(d=>d.self);
 if(device&&device!==self?.mac){root.append(el('p','File and database context is currently available for compai only.','muted'));return;}
 const context=data.process_resources;
 if(context?.status!=='live'||Date.now()/1000-context.sampled_at>=10){root.append(el('p','File and database context unavailable.','muted'));return;}
 const scores=new Map();for(const f of flowRows)for(const p of f.recent_processes||[])scores.set(p.pid,(scores.get(p.pid)||0)+f.rate);
 const processes=Object.values(context.processes||{}).filter(p=>scores.has(p.pid)||p.database_files||p.database_endpoints?.length).sort((a,b)=>(scores.get(b.pid)||0)-(scores.get(a.pid)||0));
 root.append(el('h3','Local files & database destinations'),el('p','On compai · '+new Date(context.sampled_at*1000).toLocaleTimeString()+'. Open descriptors show permitted access, not proven transfer destinations. No network rate is assigned to individual files.','muted resource-explanation'));
 for(const p of processes.slice(0,12)){
  const card=el('details',undefined,'resource-process');card.open=opened.has(p.pid);
  const summary=el('summary'),identity=el('span',processLabel(p,data));
  identity.title=p.script||p.executable||p.name;
  const info=el('small',(p.status==='available'?p.file_count+' open files · '+p.database_files+' database filename hints':'File descriptors unavailable')+' · '+(p.database_endpoints?.length||0)+' database endpoint hints');
  summary.append(identity,info);card.append(summary);
  const preview=el('div',undefined,'resource-preview');
  for(const file of (p.files||[]).filter(f=>f.database_hint).slice(0,2)){const chip=el('span',file.filename);chip.title=file.path+' · '+file.database_hint;preview.append(chip);}
  for(const endpoint of (p.database_endpoints||[]).slice(0,2))preview.append(el('span',endpoint.engine_hint+'? '+endpoint.address+':'+endpoint.port));
  if(preview.childNodes.length)summary.append(preview);
  const columns=el('div',undefined,'resource-columns');
  for(const [key,title]of [['readable','Open for reading · possible source'],['writable','Open for writing · possible receiver']]){
   const column=el('div');column.append(el('h4',title));const files=(p.files||[]).filter(f=>f[key]);
   for(const file of files){const row=el('div',undefined,'resource-file');row.append(el('code',file.filename),el('small',file.path));if(file.database_hint)row.append(el('small',file.database_hint,'resource-db-hint'));column.append(row);}
   if(!files.length)column.append(el('p',p.status==='available'?'No matching open regular files observed.':'Process file descriptors unavailable.','muted'));
   columns.append(column);
  }
  const databases=el('div');databases.append(el('h4','Database connections · port hints'));
  for(const db of p.database_endpoints||[]){const row=el('div',undefined,'resource-file');row.append(el('strong',db.engine_hint+' candidate'),el('code',(db.address.includes(':')?'['+db.address+']':db.address)+':'+db.port),el('small',(db.role||'Database-port endpoint')+' · database name unknown · '+db.evidence));databases.append(row);}
  if(!p.database_endpoints?.length)databases.append(el('p','No connected TCP database-port candidates observed. SQLite filename hints appear with open files.','muted'));
  columns.append(databases);card.append(columns);
  if(p.files_truncated||p.fd_scan_limited)card.append(el('p','Showing '+p.files.length+' of '+p.file_count+' observed unique files'+(p.fd_scan_limited?' · descriptor scan limit reached':'')+'. Database filenames are prioritized.','muted resource-explanation'));
  card.addEventListener('toggle',()=>{if(!card.isConnected)return;card.open?opened.add(p.pid):opened.delete(p.pid);});root.append(card);
 }
 if(!processes.length)root.append(el('p','No current process resources identified.','muted'));
 if(processes.length>12)root.append(el('p','Showing the top 12 process contexts; '+processes.length+' available.','muted'));
}
