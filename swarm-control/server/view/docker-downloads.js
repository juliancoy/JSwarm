const el=(tag,text,cls)=>{const node=document.createElement(tag);if(text!==undefined)node.textContent=text;if(cls)node.className=cls;return node;};
let target=null,snapshot=null,metric='network',run=null,contextAt=0;
const names={pulling:'Registry pull',distributing_layers:'LAN image transfer',publishing:'Publishing image',cached:'Cached image',ready:'Image ready',building:'Building image'};
const duration=seconds=>seconds<60?Math.ceil(seconds)+'s':seconds<3600?Math.ceil(seconds/60)+'m':Math.floor(seconds/3600)+'h '+Math.ceil(seconds%3600/60)+'m';
const bytes=value=>(value/1048576).toFixed(1)+' MiB';
function transferDetail(progress,now){
 if(progress.phase!=='distributing_layers')return '';
 if(!Number.isFinite(progress.updated)||now-progress.updated>=15)return 'Transfer ETA unavailable · waiting for progress';
 if(progress.transfer_phase==='finalizing')return 'Finalizing layers · ETA unavailable';
 const eta=Number.isFinite(progress.eta_seconds)&&progress.eta_seconds>=0?'~'+duration(progress.eta_seconds):'estimating…';
 return 'Transfer ETA '+eta+(progress.bytes_total>0?' · '+bytes(progress.bytes_completed)+' / '+bytes(progress.bytes_total)+' known layers':'')+(progress.bytes_per_second>0?' · '+bytes(progress.bytes_per_second)+'/s':'');
}
function draw(){
 if(!target)return;target.replaceChildren();target.hidden=metric!=='network';if(target.hidden)return;
 const now=Date.now()/1000,feed=snapshot?.docker_downloads;
 const fresh=feed?.status==='available'&&now-feed.sampled_at<15;
 const images=new Map();
 const coordinatorLive=now-contextAt<8&&!document.getElementById('history')?.value;
 const progress=coordinatorLive?run?.image_progress:null;
 if(progress&&typeof progress.image==='string'&&(run.status==='running'||now-Date.parse(run.updated)/1000<900)){
  const active=Boolean(['running','preparing'].includes(run.status)&&['pulling','distributing_layers','publishing','building'].includes(progress.phase));
  images.set(progress.image,{image:progress.image,state:active?names[progress.phase]:'Last coordinator image · '+(names[progress.phase]||progress.phase),
   detail:[[progress.source,progress.destination].filter(Boolean).join(' → '),active?transferDetail(progress,now):''].filter(Boolean).join(' · '),active,evidence:'Exact image reference reported by the distributed coordinator; transfer progress comes from the destination agent'});
 }
 if(fresh){
  for(const pull of feed.active||[])if(!images.has(pull.image))images.set(pull.image,{...pull,state:'Pull command active',detail:'PID '+pull.pid+' · daemon context unverified',active:true});
  for(const pull of feed.recent||[])if(!images.has(pull.image))images.set(pull.image,{...pull,state:'Recent engine pull event',detail:new Date(pull.observed_at*1000).toLocaleTimeString(),active:false});
 }
 const head=el('div',undefined,'docker-pull-heading');head.append(el('strong','Docker images'),el('small',fresh?'Host observations + coordinator'+(feed.engine_status!=='available'?' · engine events unavailable':''):'Coordinator observations · host telemetry unavailable'));target.append(head);
 if(!images.size)target.append(el('p',fresh?'No observed pull commands or recent pull events. Docker API, Compose and BuildKit activity may be unobserved.':'No coordinator image activity observed. General Docker pull telemetry is unavailable.','muted'));
 for(const image of [...images.values()].slice(0,8)){
  const card=el('div',undefined,'docker-pull-item');card.dataset.active=String(image.active);card.title=image.evidence;
  const identity=el('div');identity.append(el('code',image.image),el('small',image.detail||'compai.local'));
  card.append(identity,el('span',image.state,'docker-pull-state'));target.append(card);
 }
 if(images.size>8)target.append(el('small','Showing 8 of '+images.size+' observed image references.'));
 target.append(el('small','Device throughput includes other traffic. LAN transfer ETA uses recent image download progress for known layers; finalization takes additional time.','docker-pull-scope'));
}
export function renderDockerDownloads(root,data,mode){target=root;snapshot=data;metric=mode;draw();}
document.addEventListener('test-context',event=>{run=event.detail?.run||null;contextAt=Date.now()/1000;draw();});
