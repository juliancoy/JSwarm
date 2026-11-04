const labels={connected:'Connected',disconnected:'Disconnected',authentication_denied:'Authentication failed',unknown:'Connection unknown'};
export function connectionRows(snapshot,now=Date.now()){
 return (snapshot?.hosts||[]).map(row=>({...row,status:snapshot.inventory_available===false||!Number.isFinite(row.checked_at)||now/1000-row.checked_at>(snapshot.stale_after_seconds||5)?'unknown':labels[row.status]?row.status:'unknown'}));
}
export class ConnectionFeed{
 constructor({read=globalThis.fetch.bind(globalThis),changed,interval=1000}){this.read=read;this.changed=changed;this.interval=interval;this.timer=null;this.controller=null;this.running=false;this.last=null;this.generation=0;}
 async refresh(){
  if(!this.running||this.controller)return;
  const generation=this.generation,controller=new AbortController();this.controller=controller;const timeout=setTimeout(()=>controller.abort(),4000);
  try{const response=await this.read('/api/v1/swarm/connections',{cache:'no-store',signal:controller.signal});if(!response.ok)throw Object.assign(Error('Connection feed unavailable'),{status:response.status});const data=await response.json();if(!Array.isArray(data?.hosts))throw Error('Invalid connection feed');if(this.running&&this.generation===generation){this.last=data;this.changed(connectionRows(data),null);}}
  catch(error){if(error.status===401||error.status===403)this.last=null;if(this.running&&this.generation===generation)this.changed(connectionRows({...this.last,inventory_available:false}),error.status===401||error.status===403?'Sign in to view machine connections.':'Connection feed unavailable · retrying');}
  finally{clearTimeout(timeout);if(this.controller===controller)this.controller=null;if(this.running&&this.generation===generation)this.timer=setTimeout(()=>this.refresh(),this.interval);}
 }
 start(){if(this.running)return;this.running=true;this.generation++;void this.refresh();}
 stop(){this.running=false;this.generation++;clearTimeout(this.timer);this.controller?.abort();this.controller=null;}
 reconnect(){this.stop();this.changed(connectionRows({...this.last,inventory_available:false}),'Rechecking machine connections…');this.start();}
}
if(globalThis.document?.getElementById('device-levels')){
 const panel=document.createElement('section');panel.className='panel machine-connections';panel.setAttribute('aria-label','Live machine connections');
 const heading=document.createElement('h2');heading.textContent='Machine connections';const summary=document.createElement('p');summary.setAttribute('role','status');summary.textContent='Checking authenticated connections…';const list=document.createElement('div');list.className='connection-grid';panel.append(heading,summary,list);document.getElementById('device-levels').before(panel);
 const feed=new ConnectionFeed({changed:(rows,error)=>{
  const message=error||`${rows.filter(r=>r.status==='connected').length} / ${rows.length} machines connected · checked every second`;
  if(summary.textContent!==message)summary.textContent=message;
  list.replaceChildren(...rows.map(row=>{const card=document.createElement('article');card.className='connection-card';card.dataset.state=row.status;const name=document.createElement('strong');name.textContent=row.host;const badge=document.createElement('span');badge.textContent=labels[row.status];const detail=document.createElement('small');detail.textContent=row.status==='connected'?(!row.ready?'Connected · agent starting':row.accepting_jobs?'Accepting jobs':'Connected · worker jobs paused'):row.last_seen?`Last verified ${new Date(row.last_seen*1000).toLocaleTimeString()}`:'No verified heartbeat yet';card.append(name,badge,detail);return card;}));
 }});
 document.addEventListener('visibilitychange',()=>document.hidden?feed.stop():feed.reconnect());
 window.addEventListener('online',()=>feed.reconnect());window.addEventListener('offline',()=>{feed.stop();summary.textContent='Browser offline · machine connections unknown';for(const card of list.children){card.dataset.state='unknown';card.querySelector('span').textContent=labels.unknown;card.querySelector('small').textContent='Connection status unavailable';}});
 if(!document.hidden)feed.start();
}
