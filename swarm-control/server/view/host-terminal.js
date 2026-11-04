import {hostCpuDetail,hostCpuContext} from './cpu-report.js';
import {snapshotFetch} from './snapshot-fetch.js';
import {containerWebsites} from './container-websites.js';
import {breadcrumbNav,setBreadcrumbs} from './breadcrumbs.js';
import {cpuColor} from './cpu-colors.js';
import {hardwareLines} from './hardware.js';
let host=null,report=null,cpuReport=null,busy=false;
const panel=document.createElement('section');panel.id='host-terminal';panel.hidden=true;panel.setAttribute('role','region');panel.setAttribute('aria-label','Machine test terminal');
const heading=document.createElement('strong'),status=document.createElement('p'),output=document.createElement('pre');
output.tabIndex=0;output.setAttribute('aria-label','Machine terminal output');
const cpuSummary=document.createElement('p'),grid=document.createElement('div'),legend=document.createElement('div');grid.className='logical-cpu-grid';grid.setAttribute('aria-label','Logical CPU utilization');legend.className='cpu-heat-legend';legend.textContent='0% · Black → Red → Orange → Yellow → White · 100%';
const containersTitle=document.createElement('h3'),containerGrid=document.createElement('div');containersTitle.textContent='Running containers';containerGrid.className='container-grid';
const hint=document.createElement('p');hint.className='preview-hint';
const machineCrumbs=breadcrumbNav('Machine inspection');
heading.textContent='Machine details';hint.textContent='Select a machine to inspect its CPU, containers and test output.';
const workerControls=document.createElement('a');workerControls.href='/workers.html';workerControls.textContent='Manage worker jobs';
panel.append(heading,workerControls,machineCrumbs,hint,cpuSummary,grid,legend,containersTitle,containerGrid,status,output);
const anchor=document.getElementById('topology')?.closest('section');
if(anchor)anchor.after(panel);else (document.querySelector('main')||document.querySelector('.layout')||document.body).append(panel);
function render(){
 if(!host)return;
 setBreadcrumbs(machineCrumbs,[{label:'Network',action:dismiss},{label:host}]);
 const machine=report?.run?.hosts?.find(item=>item.host===host);
 heading.textContent=host+' · CPU & test terminal';
 const cpu=cpuReport?.hosts?.find(item=>item.host===host);
 cpuSummary.textContent=cpu?hostCpuDetail(cpu)+'\n'+hostCpuContext(cpu):'CPU measurements unavailable';
 cpuSummary.textContent+='\n'+hardwareLines(cpu?.hardware).join('\n');cpuSummary.style.whiteSpace='pre-line';
 grid.replaceChildren();
 if(cpu?.status==='live')for(const core of cpu.logical_cpus||[]){
  const cell=document.createElement('div');cell.className='logical-cpu-cell';cell.style.backgroundColor=cpuColor(core.percent);cell.style.color=core.percent>=65?'#171009':'#fff';
  cell.textContent='CPU '+core.id+'\n'+(core.percent==null?'Unknown':core.percent.toFixed(1)+'%');
  cell.title='Logical CPU '+core.id+' · '+(core.percent==null?'Unavailable':core.percent+'% utilization');grid.append(cell);
 }

 status.textContent=machine?(machine.status+' · '+(report.run.finished?'Recorded output':'Live output · updates every second')):'No distributed test output for this machine.';
 const jobs=Object.values(machine?.jobs||{}),active=jobs.filter(job=>job.status==='running');
 const text=(active.length?active:jobs).map(job=>'── '+job.name+' · '+job.status+' · '+(job.phase||'waiting')+' ──\n'+((job.logs||[]).join('\n')||'Waiting for output…')).join('\n\n')||'Start application tests to collect output. This panel shows test process output, not an interactive shell.';
 const follow=output.scrollHeight-output.scrollTop-output.clientHeight<40;
 if(output.textContent!==text){output.textContent=text;if(follow)output.scrollTop=output.scrollHeight;}
}
async function refresh(){
 if(!host||busy)return;busy=true;
 const results=await Promise.allSettled(['/api/distributed-tests','/api/cpu'].map(async url=>{const response=await snapshotFetch(url,{cache:'no-store'});if(!response.ok)throw Error();return response.json();}));
 report=results[0].status==='fulfilled'?results[0].value:null;
 cpuReport=results[1].status==='fulfilled'?results[1].value:null;
 render();if(results[0].status==='rejected')status.textContent='Disconnected · could not refresh terminal output';busy=false;
}
function openHost(name){
 const changed=host!==name;host=name;panel.hidden=false;
 if(changed){output.textContent='';render();void refresh();}
}
export function selectHostTerminal(name){if(name)openHost(name);}
function dismiss(){panel.hidden=true;host=null;heading.textContent='Machine details';status.textContent='Select a machine.';output.textContent='';grid.replaceChildren();containerGrid.replaceChildren();cpuSummary.textContent='';clearWebsite();selectedContainer=null;containerPanel.hidden=true;}
document.addEventListener('click',event=>{const node=event.target.closest('[data-terminal-host]');if(node)selectHostTerminal(node.dataset.terminalHost);});
setInterval(()=>{if(!panel.hidden&&document.visibilityState==='visible')void refresh();},1000);

let selectedContainer=null,containerHost=null,containerBusy=false;
const containerClass=item=>item.name.endsWith('-launcher')?'launcher':'application';
const containerPanel=document.createElement('section');containerPanel.id='container-hover';containerPanel.hidden=true;containerPanel.setAttribute('role','region');containerPanel.setAttribute('aria-label','Container details and console');
const containerHeading=document.createElement('strong'),containerDetails=document.createElement('pre'),containerConsole=document.createElement('pre'),containerClose=document.createElement('button');
containerClose.textContent='Close container';containerClose.type='button';containerConsole.tabIndex=0;containerConsole.setAttribute('aria-label','Container console output');
const consoleTools=document.createElement('div'),consoleSearch=document.createElement('input'),consolePause=document.createElement('button'),consoleInfo=document.createElement('span');
consoleTools.className='console-tools';consoleSearch.type='search';consoleSearch.placeholder='Filter console output';consoleSearch.setAttribute('aria-label','Filter console output');consolePause.type='button';consolePause.textContent='Pause output';let consolePaused=false,consoleText='';
function renderConsole(){const query=consoleSearch.value.toLowerCase(),lines=consoleText.split('\n');const visible=query?lines.filter(line=>line.toLowerCase().includes(query)):lines;const follow=containerConsole.scrollHeight-containerConsole.scrollTop-containerConsole.clientHeight<40;containerConsole.textContent=visible.join('\n')||'No matching output';consoleInfo.textContent=consolePaused?'Paused':query?visible.length+' matching lines':'Live · latest 100 lines';if(follow)containerConsole.scrollTop=containerConsole.scrollHeight;}
consoleSearch.oninput=renderConsole;consolePause.onclick=()=>{consolePaused=!consolePaused;consolePause.textContent=consolePaused?'Resume output':'Pause output';renderConsole();};
consoleTools.append(consoleSearch,consolePause,consoleInfo);
const containerViews=document.createElement('div'),consoleView=document.createElement('button'),websiteView=document.createElement('button');
containerViews.className='container-view-options';containerViews.setAttribute('role','group');containerViews.setAttribute('aria-label','Container view');
consoleView.type=websiteView.type='button';consoleView.textContent='Console';websiteView.textContent='Website';containerViews.append(consoleView,websiteView);
const website=document.createElement('section'),websiteForm=document.createElement('form'),websitePorts=document.createElement('select'),websiteUrl=document.createElement('input'),websiteGo=document.createElement('button'),websiteRefresh=document.createElement('button'),websiteLink=document.createElement('a'),websiteHint=document.createElement('p'),websiteFrame=document.createElement('iframe');
website.className='container-website';website.hidden=true;websiteForm.className='container-website-tools';websitePorts.setAttribute('aria-label','Published website port');websiteUrl.type='url';websiteUrl.placeholder='Website or gateway URL';websiteUrl.setAttribute('aria-label','Container website URL');websiteGo.type='submit';websiteGo.textContent='View';websiteRefresh.type='button';websiteRefresh.textContent='Reload';websiteLink.textContent='Open in new tab';websiteLink.target='_blank';websiteLink.rel='noopener noreferrer';websiteHint.className='muted';websiteFrame.title='Live container website';websiteFrame.hidden=true;websiteFrame.setAttribute('sandbox','allow-scripts allow-forms allow-same-origin allow-popups');websiteFrame.referrerPolicy='no-referrer';
websiteForm.append(websitePorts,websiteUrl,websiteGo,websiteRefresh,websiteLink);website.append(websiteForm,websiteHint,websiteFrame);
let containerView='console',websiteKey='',websiteDetail=null;const websiteUrls=new Map();
function renderWebsite(detail){
 websiteDetail=detail;
 const key=host+'/'+selectedContainer,endpoints=containerWebsites(host,detail),signature=JSON.stringify(endpoints),changed=websiteKey!==key;
 if(changed){
  websiteKey=key;websiteUrl.value=websiteUrls.get(key)||endpoints.find(item=>!item.loopback)?.url||'';
  websiteFrame.removeAttribute('src');websitePorts.dataset.signature='';
 }
 if(websitePorts.dataset.signature!==signature){
  websitePorts.replaceChildren(new Option('Custom URL',''));
  for(const endpoint of endpoints){const option=new Option(endpoint.label,endpoint.url);option.disabled=endpoint.loopback;websitePorts.append(option);}
  websitePorts.dataset.signature=signature;
 }
 websitePorts.value=endpoints.some(item=>item.url===websiteUrl.value&&!item.loopback)?websiteUrl.value:'';
 websitePorts.hidden=!endpoints.length;
 websiteHint.textContent=endpoints.some(item=>!item.loopback)?'Live, interactive website. If the site blocks embedding, open it in a new tab.':endpoints.length?'Published ports are bound to this machine’s loopback address. Enter a reachable gateway URL to preview the site.':'No published web endpoint. Enter a reachable website or gateway URL, or use Console for this container.';
 if(containerView==='website'&&changed&&websiteUrl.value)loadWebsite();
}
function loadWebsite(){
 let url;try{url=new URL(websiteUrl.value);if(!['http:','https:'].includes(url.protocol)||url.username||url.password)throw Error();}catch{websiteHint.textContent='Enter an HTTP or HTTPS website URL.';return;}
 websiteUrls.set(websiteKey,url.href);websiteLink.href=url.href;websiteLink.hidden=false;websiteFrame.hidden=false;
 if(websiteFrame.getAttribute('src')!==url.href)websiteFrame.src=url.href;
}
function setContainerView(view){
 containerView=view;containerPanel.dataset.view=view;website.hidden=view!=='website';consoleTools.hidden=containerConsole.hidden=view!=='console';containerDetails.hidden=view==='website';
 consoleView.setAttribute('aria-pressed',String(view==='console'));websiteView.setAttribute('aria-pressed',String(view==='website'));
 if(view==='website'&&websiteDetail){renderWebsite(websiteDetail);if(websiteUrl.value)loadWebsite();}else websiteFrame.removeAttribute('src');
}
function clearWebsite(){websiteFrame.removeAttribute('src');websiteFrame.hidden=true;websiteLink.hidden=true;websiteKey='';websiteDetail=null;}
consoleView.onclick=()=>setContainerView('console');websiteView.onclick=()=>setContainerView('website');
websiteForm.onsubmit=event=>{event.preventDefault();loadWebsite();};websitePorts.onchange=()=>{if(websitePorts.value){websiteUrl.value=websitePorts.value;loadWebsite();}};
websiteRefresh.onclick=()=>{websiteFrame.removeAttribute('src');loadWebsite();};websiteLink.hidden=true;setContainerView('console');
const containerCrumbs=breadcrumbNav('Container inspection');
containerPanel.append(containerClose,containerHeading,containerCrumbs,containerViews,containerDetails,consoleTools,containerConsole,website);panel.append(containerPanel);
function backToMachine(){clearWebsite();selectedContainer=null;containerPanel.hidden=true;selectHostTerminal(host);heading.tabIndex=-1;heading.focus({preventScroll:true});}
containerClose.onclick=backToMachine;

async function refreshContainers(){
 if(!host||panel.hidden||containerBusy)return;containerBusy=true;
 const requestedHost=host,requestedId=selectedContainer;
 try{
  const response=await fetch('/api/containers?host='+encodeURIComponent(requestedHost)+(requestedId?'&id='+requestedId:''),{cache:'no-store'});
  if(!response.ok)throw Error();const data=await response.json();
  if(host!==requestedHost||selectedContainer!==requestedId)return;
  containersTitle.textContent='Running containers · '+(data.status==='live'?data.containers.length:'Unavailable');
  const signature=JSON.stringify(data.containers);
  if(containerGrid.dataset.signature!==signature){
   containerGrid.dataset.signature=signature;containerGrid.replaceChildren();
   for(const item of data.containers){
    const button=document.createElement('button');button.type='button';button.className='container-tile';button.dataset.containerClass=containerClass(item);button.textContent=item.name+'\n'+(containerClass(item)==='launcher'?'Launcher · Biotech gold\n':'')+item.image;button.setAttribute('aria-controls','container-hover');
    const open=()=>{if(selectedContainer===item.id&&!containerPanel.hidden)return;clearWebsite();selectedContainer=item.id;renderWebsite(item);setContainerView(containerView);containerHeading.textContent=item.name;containerDetails.textContent=item.image;containerConsole.textContent='Loading console…';containerPanel.hidden=false;setBreadcrumbs(containerCrumbs,[{label:'Network',action:()=>{dismiss();clearWebsite();containerPanel.hidden=true;selectedContainer=null;}},{label:host,action:backToMachine},{label:item.name}]);setTimeout(()=>void refreshContainers(),0);};
    button.onclick=open;containerGrid.append(button);
   }
  }
  if(requestedId&&!data.detail){clearWebsite();websiteHint.textContent='Container stopped or is no longer available.';containerDetails.textContent='Container stopped or is no longer available.';containerConsole.textContent='';}
  if(data.detail){
   const detail=data.detail;renderWebsite(detail);containerHeading.textContent=detail.name+' · '+requestedHost;
   containerPanel.dataset.containerClass=containerClass(detail);
   containerDetails.textContent=JSON.stringify({image:detail.image,class:containerClass(detail),id:detail.id,state:detail.state,health:detail.health,started:detail.started_at,restarts:detail.restart_count,ports:detail.ports},null,2);
   if(!consolePaused){consoleText=detail.console||'No console output recorded.';renderConsole();}
  }
 }catch{if(host===requestedHost){containersTitle.textContent='Running containers · Disconnected';containerGrid.replaceChildren();containerGrid.dataset.signature='';if(requestedId)containerConsole.textContent='Console unavailable · retrying…';}}
 finally{containerBusy=false;}
}
setInterval(()=>{
 if(!host||panel.hidden){clearWebsite();containerPanel.hidden=true;selectedContainer=null;containerHost=null;return;}
 if(containerHost!==host){clearWebsite();containerHost=host;selectedContainer=null;containerPanel.hidden=true;containerGrid.replaceChildren();containerGrid.dataset.signature='';containersTitle.textContent='Running containers · Loading…';}
 if(document.visibilityState==='visible')void refreshContainers();
},1000);
document.addEventListener('keydown',event=>{if(event.key==='Escape'){clearWebsite();selectedContainer=null;containerPanel.hidden=true;}});
