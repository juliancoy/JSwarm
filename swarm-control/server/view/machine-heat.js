import {hostCpuDetail,hostCpuContext,percent} from './cpu-report.js';
import {cpuColor} from './cpu-colors.js';
import {hardwareLines} from './hardware.js';
let hosts=[];
function decorate(){
 for(const node of document.querySelectorAll('#topology [data-terminal-host]')){
  const host=hosts.find(h=>h.host===node.dataset.terminalHost);
  let grid=node.querySelector('.machine-heat'),caption=node.querySelector('.machine-load');
  if(!grid){grid=document.createElement('span');grid.className='machine-heat';caption=document.createElement('small');caption.className='machine-load';node.append(grid,caption);}
  const signature=JSON.stringify(host);
  if(grid.dataset.sample===signature)continue;grid.dataset.sample=signature;
  grid.replaceChildren();
  if(host?.status==='live')for(const core of host.logical_cpus||[]){const tile=document.createElement('span');tile.style.backgroundColor=cpuColor(core.percent);tile.title='CPU '+core.id+': '+(core.percent??'unknown')+'%';grid.append(tile);}
  caption.textContent=host?.status==='live'?percent(host.percent)+' CPU · '+host.cpus+' logical CPUs':'CPU '+(host?.status||'measuring…');
  caption.title=host?hostCpuDetail(host)+' · '+hostCpuContext(host):'No CPU measurement yet';
  let hardware=node.querySelector('.machine-hardware');
  if(!hardware){hardware=document.createElement('small');hardware.className='machine-hardware';node.append(hardware);}
  hardware.textContent=hardwareLines(host?.hardware).join('\n');
 }
}
document.addEventListener('cpu-sample',event=>{hosts=event.detail.hosts;decorate();});
const topology=document.getElementById('topology');if(topology)new MutationObserver(records=>{if(records.some(record=>record.target===topology))decorate();}).observe(topology,{childList:true});
