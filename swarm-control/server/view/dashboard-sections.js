const sections={overview:'Task Manager',traffic:'Traffic',runs:'Runs & logs',tools:'Devices & tools'};
const params=new URLSearchParams(location.search);
const current=Object.hasOwn(sections,params.get('view'))?params.get('view'):'overview';
const nav=document.createElement('nav');nav.className='dashboard-sections';nav.setAttribute('aria-label','Dashboard sections');
for(const [key,title]of Object.entries(sections)){const a=document.createElement('a');const query=new URLSearchParams(params);if(key==='overview')query.delete('view');else query.set('view',key);a.href='/tests.html'+(query.size?'?'+query:'');a.textContent=title;if(key===current)a.setAttribute('aria-current','page');nav.append(a);}
document.querySelector('.command-bar').after(nav);
const groups={
 traffic:['throughput-sources','congestion'],
 runs:['status','error','run-activity','snapshot-staging','run-history','terminal-job','matrix','timeline','jobs','scope'],
 tools:['mobile-devices','codex-context','cpu-readout']
};
for(const [group,ids]of Object.entries(groups))for(const id of ids){const el=document.getElementById(id);if(!el)continue;const panel=el.closest('main > section, main > footer')||el;panel.hidden=current!==group;}
for(const id of ['device-levels','overall-progress','summary','topology']){const el=document.getElementById(id);const panel=el?.closest('main > section')||el;if(panel)panel.hidden=id==='device-levels'?!['overview','traffic'].includes(current):current!=='runs';}
const intro=document.querySelector('.intro');if(intro)intro.hidden=current!=='runs';
const map=document.querySelector('.lan-particles');if(map){map.hidden=true;map.querySelector('iframe')?.remove();}
const title=document.querySelector('header h1');title.textContent=current==='overview'?'Global Task Manager':sections[current];
document.title=title.textContent+' · LAN ecosystem';
if(['overview','traffic'].includes(current))void import('./levels.js');
if(current==='traffic'){void import('./throughput.js');void import('./congestion.js');}
if(current==='tools'){void import('./cpu.js');void import('./codex.js');void import('./mobile-devices.js');}
for(const panel of document.querySelectorAll('main>section[hidden],main>footer[hidden],#cpu-readout[hidden]'))panel.classList.add('dashboard-hidden');
document.addEventListener('click',event=>{const link=event.target.closest('a[href^="#"]');if(!link)return;const target=document.getElementById(link.getAttribute('href').slice(1));if(target?.closest('.dashboard-hidden')){event.preventDefault();const query=new URLSearchParams(location.search);query.set('view','runs');location.href='/tests.html?'+query+link.getAttribute('href');}});

if(current==='overview'){
 document.body.classList.add('task-manager');
 const main=document.querySelector('main'),usage=document.getElementById('device-levels');main.prepend(usage);
 for(const id of ['summary','topology']){const el=document.getElementById(id);(el.closest('main > section')||el).classList.add('dashboard-hidden');}
 document.querySelector('.command-bar').classList.add('dashboard-hidden');
 const eyebrow=document.querySelector('header .eyebrow');if(eyebrow)eyebrow.textContent='All machines · Live resource usage';
}
