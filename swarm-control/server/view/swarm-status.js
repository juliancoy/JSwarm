import {machineCards,stateLabels} from './swarm-machines.js';
const $=id=>document.getElementById(id),feedback=$('feedback');let loading=false,current=null,gridSignature='',detailSignature='';
function element(tag,text='',cls){const node=document.createElement(tag);node.textContent=text;if(cls)node.className=cls;if(tag==='h2')node.tabIndex=-1;return node}
function date(value){if(!value)return 'Not recorded';const parsed=new Date(value);return Number.isNaN(parsed.valueOf())?'Not recorded':parsed.toLocaleString()}
function reachability(card){return card.reachable===true?'Ping responded':card.reachable===false?'No ping response':'Reachability not observed'}
function render(){
 if(!current)return;
 const cards=machineCards(current),selected=new URLSearchParams(location.hash.slice(1)).get('machine');
 $('overview').hidden=!!selected;$('machine-detail').hidden=!selected;
 const signature=JSON.stringify(cards);
 if(gridSignature!==signature){
  $('devices').replaceChildren(...cards.map(card=>{
   const link=element('a','','status-card machine-card');link.href='#machine='+encodeURIComponent(card.id);link.dataset.state=card.state;
   link.append(element('span',stateLabels[card.state],'machine-state'),element('h3',card.name),element('p',card.address||'Address not recorded'));
   const counts=Object.entries(card.counts);link.append(element('p',counts.length?counts.map(([state,count])=>`${count} ${stateLabels[state].toLowerCase()}`).join(' · '):'No tasks recorded'));
   link.append(element('small',reachability(card)),element('span','View machine →','machine-open'));return link
  }));
  if(!cards.length)$('devices').append(element('p','No machines have been observed yet.'));
  gridSignature=signature;
 }
 $('observed').textContent=`${cards.length} machines · Network observed ${date(current.updated)}`;
 $('run').textContent=`${current.run.finished?'Recorded run':'Current run'}: ${current.run.status}${current.run.id?' · '+current.run.id:''}`;
 if(selected){
  const card=cards.find(card=>card.id===selected),signature=JSON.stringify([selected,card,current.run.id,current.run.status]);
  if(detailSignature!==signature){
   const content=$('machine-content');content.replaceChildren();
   if(!card){content.append(element('h2','Machine not in the current snapshot'),element('p','Return to all machines or refresh to check again.'))}
   else{
    content.append(element('h2',card.name),element('p',card.address||'Address not recorded'),element('p',reachability(card)),element('p',`Task state: ${stateLabels[card.state]}`));
    if(card.coordinator)content.append(element('p','Swarm coordinator'));
    if(card.cpus)content.append(element('p',`${card.cpus} logical CPUs${card.memory_mb?' · '+card.memory_mb+' MB memory':''}`));
    content.append(element('h3','Tasks'),element('p',current.run.finished?'Results from the recorded run.':'Latest task observations.'));
    const tasks=element('div','','machine-tasks');
    for(const job of card.jobs){const row=element('article','','machine-task');row.dataset.state=job.state;row.append(element('strong',job.name),element('span',stateLabels[job.state]));tasks.append(row)}
    if(!card.jobs.length)tasks.append(element('p','No tasks recorded for this machine.'));
    content.append(tasks);
   }
   detailSignature=signature;
  }
 }
}
async function refresh(){
 if(loading)return;loading=true;$('refresh').disabled=true;
 try{
  const response=await fetch('/api/v1/swarm/status',{cache:'no-store',signal:AbortSignal.timeout(10000)});
  if(response.status===401){current=null;gridSignature='';detailSignature='';for(const id of ['devices','interfaces','network','observed','run','machine-content'])$(id).replaceChildren();throw Error('Pair this browser in Swarm Control to view your swarm.');}
  if(!response.ok)throw Error('Swarm status is unavailable. Try Refresh.');
  current=await response.json();
  $('network').textContent=`${current.network.interface||'Interface not recorded'} · ${current.network.subnet||'Subnet not recorded'} · Gateway ${current.network.gateway||'not recorded'}`;
  const rate=value=>Number.isFinite(value)?value.toFixed(2)+' Mb/s':'Not measured';
  $('interfaces').replaceChildren(...current.network.interfaces.map(link=>{const card=element('article','','status-card');card.append(element('h3',link.name),element('p',`Receive ${rate(link.rx_mbps)} · Send ${rate(link.tx_mbps)}`),element('small',`Reported capacity ${rate(link.capacity_mbps)} · Measured ${date(link.updated)}`));return card}));
  render();feedback.dataset.error='false';feedback.textContent=`Status refreshed ${new Date().toLocaleTimeString()}${current.scanning?' · Network scan in progress':''}`;
 }catch(error){feedback.dataset.error='true';feedback.textContent=error.message+(current?' Displayed observations may be out of date.':'')}
 finally{loading=false;$('refresh').disabled=false}
}
let selectedMachine=null;
window.addEventListener('hashchange',()=>{const selected=new URLSearchParams(location.hash.slice(1)).get('machine');render();if(selected){selectedMachine=selected;$('machine-content').querySelector('h2')?.focus()}else{[...$('devices').querySelectorAll('a')].find(link=>link.getAttribute('href')==='#machine='+encodeURIComponent(selectedMachine))?.focus()}});
$('refresh').onclick=refresh;void refresh();setInterval(()=>{if(!document.hidden)void refresh()},10000);

$('run-tests').onclick=async()=>{
 const button=$('run-tests');button.disabled=true;
 try{const response=await fetch('/api/v1/swarm/tests/request',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});const value=await response.json();if(!response.ok)throw Error(value.error?.message||'Could not queue tests');$('test-request').textContent='Test request queued: '+value.id;await refresh();}
 catch(error){$('test-request').textContent=error.message}finally{button.disabled=false}
};
