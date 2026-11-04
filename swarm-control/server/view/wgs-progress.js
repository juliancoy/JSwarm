const gib=value=>(value/1024**3).toFixed(1)+' GiB';
const section=document.createElement('section');section.className='panel wgs-progress';section.setAttribute('aria-label','Live whole genome processing');
const title=document.createElement('h2');title.textContent='Whole-genome processing';const status=document.createElement('p');status.setAttribute('role','status');status.textContent='Loading work streams…';const streams=document.createElement('div');streams.className='connection-grid';const phases=document.createElement('p');phases.className='muted';const details=document.createElement('details');const summary=document.createElement('summary');summary.textContent='Cluster storage allocation';const storage=document.createElement('div');storage.className='wgs-storage';details.append(summary,storage);section.append(title,status,streams,phases,details);const mount=document.getElementById('wgs-estimator');
const endpoint=mount?.dataset.source||'/api/v1/swarm/wgs';
if(mount)mount.append(section);else document.getElementById('device-levels').before(section);
let timer,busy=false,controller;
function text(tag,value){const node=document.createElement(tag);node.textContent=value;return node}
function duration(seconds){return seconds<3600?Math.max(1,Math.ceil(seconds/60))+' min':(seconds/3600).toFixed(1)+' h'}
function etaLabel(eta){
 if(!eta)return 'Transfer ETA: collecting observations';
 if(eta.status==='complete')return 'Transfer complete · analysis and validation have separate completion times';
 if(eta.status==='estimating'){
  const range=eta.pessimistic_seconds===null?'upper duration unknown':duration(eta.optimistic_seconds)+'–'+duration(eta.pessimistic_seconds);
  return `Transfer ETA: about ${duration(eta.seconds_remaining)} · observed-rate range ${range}. Excludes analysis and capacity changes.`;
 }
 const labels={verifying_cache:'verifying cached chunks on disk',blocked:'waiting for capacity or receiver retry',stale:'progress report stale',stalled:'no recent completed-chunk progress',insufficient_data:'collecting at least 60 seconds of progress'};
 return 'Transfer ETA unavailable · '+(labels[eta.status]||'collecting observations');
}
async function refresh(){
 if(document.hidden||busy)return;busy=true;controller=new AbortController();const timeout=setTimeout(()=>controller.abort(),8000);
 try{const response=await fetch(endpoint,{cache:'no-store',signal:controller.signal});if(!response.ok)throw Error(response.status===401?'Sign in to view processing progress.':'Processing feed unavailable · retrying');const data=await response.json();section.hidden=!data.available;if(!data.available)return;
 status.textContent=`${data.download_status==='running'?'Streaming downloads and analysis':data.download_status.replaceAll('_',' ')} · updates every 5 seconds`;
 streams.replaceChildren(...data.samples.map(sample=>{const card=document.createElement('article');card.className='connection-card';const progress=document.createElement('progress');progress.max=sample.bytes_total||1;progress.value=sample.bytes_received;progress.setAttribute('aria-label',sample.sample+' bytes received');card.append(text('strong',sample.sample),text('span',`${sample.files_verified} / ${sample.files_total} files checksum verified · ${sample.files_active} downloading${sample.files_failed?' · '+sample.files_failed+' awaiting retry':''}`),progress,text('small',`${gib(sample.bytes_received)} received / ${gib(sample.bytes_total)} expected`),text('small',etaLabel(sample.eta)));return card}));
 const index=data.index.characters_processed&&data.index.characters_total?` · BWT pass ${(data.index.characters_processed/data.index.characters_total*100).toFixed(1)}%`:'';
 const gpu=data.gpu_qc&&data.gpu_qc.status!=='not_started'?` · Vulkan QC: ${data.gpu_qc.status.replaceAll('_',' ')} · ${(data.qc.gpu_bases||0).toLocaleString()} bases in ${(data.qc.gpu_batches||0).toLocaleString()} streaming batches`:'';
 const backfill=data.gpu_backfill&&data.gpu_backfill.status!=='not_started'?` · GPU histogram backfill: ${data.gpu_backfill.verified_files} files verified · ${data.gpu_backfill.bases.toLocaleString()} bases · ${data.gpu_backfill.cpu_crosschecked_batches} batches independently CPU checked`:'';
 phases.textContent=`Reference index: ${data.index.status}${data.index.status==='complete'?'':index} · Read QC: ${data.qc.reads_processed.toLocaleString()} reads processed${gpu}${backfill} · Alignments: ${data.alignment.completed} completed · ${data.alignment.status.replaceAll('_',' ')}. Streaming QC is provisional until source checksum verification.`;
 storage.replaceChildren(...data.storage.map(row=>text('p',`${row.host}: ${gib(row.assigned_bytes)} input assigned · ${gib(row.free_bytes_at_start)} free at preflight. Analysis scratch is checked separately.`)));
 }catch(error){section.hidden=false;status.textContent=error.message;streams.replaceChildren();phases.textContent='Current processing status is unknown until the connection recovers.';storage.replaceChildren();}
 finally{clearTimeout(timeout);busy=false;clearTimeout(timer);if(!document.hidden)timer=setTimeout(refresh,5000)}
}
document.addEventListener('visibilitychange',()=>{clearTimeout(timer);if(document.hidden)controller?.abort();else void refresh()});window.addEventListener('online',()=>{clearTimeout(timer);void refresh()});void refresh();
