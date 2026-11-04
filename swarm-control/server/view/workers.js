const list=document.getElementById('worker-list'),status=document.getElementById('worker-status'),refresh=document.getElementById('worker-refresh');
let busy=false,loading=false;
async function load(){
 if(busy||loading)return;loading=true;refresh.disabled=true;
 try{
  const r=await fetch('/api/worker-controls',{cache:'no-store'});if(!r.ok)throw Error();const data=await r.json();
  const focus=document.activeElement?.dataset.key;
  list.replaceChildren(...data.hosts.map(row=>{
   const card=document.createElement('article');card.className='worker-card';
   const heading=document.createElement('h2');heading.textContent=row.host;
   const meta=document.createElement('p');meta.className='worker-meta';
   const unavailable=row.mode==='unavailable',paused=row.mode==='paused';
   meta.textContent=(unavailable?'Unavailable · status could not be verified':paused?'Paused · no new jobs':'Automatic participation')+(row.active_workers==null?'':` · ${row.active_workers} active swarm worker${row.active_workers===1?'':'s'}`);
   const buttons=document.createElement('div');buttons.className='worker-buttons';
   for(const [action,label] of [['pause','Pause new jobs'],['stop','Stop swarm work now'],['resume','Resume jobs']]){
    const b=document.createElement('button');b.type='button';b.textContent=label;b.dataset.key=row.host+':'+action;b.disabled=unavailable||(action==='pause'&&paused);b.setAttribute('aria-label',label+' on '+row.host);if(action==='stop')b.className='stop-worker';b.onclick=()=>act(row.host,action);buttons.append(b);
   }
   card.append(heading,meta,buttons);return card;
  }));
  if(focus)Array.from(list.querySelectorAll('button')).find(b=>b.dataset.key===focus&&!b.disabled)?.focus();
  status.textContent='Status checked at '+new Date().toLocaleTimeString()+'.';
 }catch{status.textContent='Could not refresh worker status. Try again.';}
 finally{loading=false;refresh.disabled=false;}
}
async function act(host,action){
 if(busy)return;busy=true;for(const b of list.querySelectorAll('button'))b.disabled=true;refresh.disabled=true;
 status.textContent=`${action==='stop'?'Stopping swarm work':action==='pause'?'Pausing new jobs':'Resuming jobs'} on ${host}…`;
 let message;
 try{
  const r=await fetch('/api/worker-controls',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({host,action})});const data=await r.json();if(!r.ok)throw Error(data.error||'Change could not be verified.');
  message=`${host}: ${action==='stop'?'swarm work stopped; new jobs paused':action==='pause'?'new jobs paused':'automatic participation restored'}.`;
 }catch(e){message=e.message;}
 finally{busy=false;await load();status.textContent=message;}
}
refresh.onclick=()=>load();void load();setInterval(()=>{if(document.visibilityState==='visible')void load();},10000);
