import {authorizedAction} from './authorized-action.js';
import {snapshotFetch} from './snapshot-fetch.js';
import {pollVisible} from './visible-poll.js';
const panel=document.getElementById('mobile-devices');
const node=(tag,text='',cls='')=>{const el=document.createElement(tag);el.textContent=text;el.className=cls;return el;};
let requesting=false,queued=false;
async function refresh(){
 if(!panel)return;
 try{
  const response=await snapshotFetch('/api/mobile-tests',{cache:'no-store',signal:AbortSignal.timeout(12000)});
  if(!response.ok)throw Error('Mobile worker discovery unavailable');
  const data=await response.json(),device=data.devices?.[0],host=data.latest?.host,job=host?.jobs?.mobile;
  const running=job?.status==='running',connected=device?.connected;
  queued=Boolean(data.requests?.length);
  if(running)queued=false;
  const card=node('div','','mobile-card');card.dataset.state=running?'running':connected?'ready':'offline';
  const phone=node('div','','mobile-phone');phone.setAttribute('aria-hidden','true');phone.append(node('span',running?'▶':job?.status==='passed'?'✓':'●'));
  const body=node('div','','mobile-body');body.append(node('div','PHYSICAL DEVICE · ANDROID CHROME','mobile-eyebrow'));
  const title=node('div','','mobile-title');title.append(node('h2',device?.name||'Pixel 3'),node('span',running?'Testing now':connected?'USB connected':'Disconnected','mobile-badge'));body.append(title);
  body.append(node('p',connected?`Android ${device.android} · USB to compai · 1 browser slot`:'Connect and authorize the Pixel over USB to run real-device checks.','mobile-meta'));
  body.append(node('p','Real screen · Touch input · Screenshots','mobile-capabilities'));
  if(device?.gpu)body.append(node('p',device.gpu.driver+' GPU · '+device.gpu.backend+(device.gpu.compute_verified?' · WebGPU compute verified '+device.gpu.validation.checked_at:' · compute untested'),'mobile-meta'));
  const result=node('div','','mobile-result');
  if(job){
   const state=job.status==='passed'?'Passed':job.status==='failed'?'Failed':job.status==='stopped'?'Stopped':job.phase||job.status;
   result.append(node('strong',state));
   result.append(node('span',` · ${job.checks?.filter(c=>c.status==='passed').length||0} mobile checks${job.duration_seconds!=null?' · '+job.duration_seconds.toFixed(1)+'s':''}`));
   if(job.failure_details?.length)result.append(node('p',job.failure_details[0]));
  }else result.textContent='Ready for its first mobile test';
  body.append(result);
  const actions=node('div','','mobile-actions'),button=node('button',queued?'Queued':running?'Running on Pixel':'Run Pixel tests');
  button.type='button';button.disabled=!connected||running||requesting||queued;
  button.onclick=async()=>{
   requesting=true;button.disabled=true;
   try{const res=await authorizedAction('/api/mobile-tests/start',{method:'POST'});if(!res.ok)throw Error(await res.text());queued=true;await refresh();}
   catch(error){result.textContent=error.message;}finally{requesting=false;}
  };
  const link=node('a','Results & screenshots');link.href='/tests.html'+(data.latest?.run_id?'?run='+encodeURIComponent(data.latest.run_id)+'&host='+encodeURIComponent(host.host)+'#details-title':'');link.className='mobile-results-link';
  actions.append(button,link);body.append(actions);
  card.append(phone,body);
  const screenshot=job?.artifacts?.find(a=>a.name==='submission-touch.png')||job?.artifacts?.find(a=>a.name.endsWith('.png'));
  if(screenshot){const url=new URL(screenshot.url,location.origin);if(url.origin===location.origin&&url.pathname==='/api/distributed-tests/artifacts'){
   const link=node('a','','mobile-preview');link.href=url.href;link.target='_blank';link.rel='noopener';const previous=panel.querySelector('.mobile-preview img');const img=previous?.src===url.href?previous:node('img');if(img.src!==url.href)img.src=url.href;img.alt='Latest screenshot from the real Pixel 3';img.loading='lazy';link.append(img);card.append(link);
  }}
  panel.replaceChildren(card);
 }catch(error){panel.replaceChildren(node('p',error.message,'mobile-error'));}
}
pollVisible(refresh,5000);
