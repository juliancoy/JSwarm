import {ArchivePeer} from './peer.js';
const $=id=>document.getElementById(id);let peer;const peers=new Map();
const token=new URLSearchParams(location.hash.slice(1)).get('token');
if (token) {$('token').value=token;history.replaceState(null,'',location.pathname);}
const showError=error=>{$('status').textContent=error.message||String(error);};
$('join').onsubmit=async event=>{
 event.preventDefault();peer?.disconnect();
 try {
  peer=new ArchivePeer({token:$('token').value.trim()});
  peer.addEventListener('status',e=>{$('status').textContent=e.detail;});
  peer.addEventListener('error',e=>showError(e.detail));
  peer.addEventListener('peer',e=>{peers.set(e.detail.id,e.detail.state);$('peers').replaceChildren(...Array.from(peers,([id,state])=>{const li=document.createElement('li');li.textContent=id.slice(0,8)+' · '+state;return li;}));});
  peer.addEventListener('progress',e=>{$('progress').max=e.detail.total||1;$('progress').value=e.detail.done;});
  peer.addEventListener('manifest',e=>{
   $('dataset').textContent=e.detail.kind+' · '+e.detail.files.length+' files';$('sync').disabled=false;$('files').replaceChildren();
   for (const file of e.detail.files) {const button=document.createElement('button');button.textContent='Save '+file.path;button.onclick=async()=>{try {const url=URL.createObjectURL(await peer.file(file.path));const a=document.createElement('a');a.href=url;a.download=file.path.split('/').pop();a.click();setTimeout(()=>URL.revokeObjectURL(url),10000);}catch(e){showError(e);}};$('files').append(button);}
  });
  await peer.connect();$('token').value='';$('package').disabled=false;$('forget').disabled=false;
  $('network').textContent=peer.relayAvailable?'Direct connections preferred; TURN fallback available.':'Direct connections and STUN available. TURN fallback is not configured.';
 } catch(e) {showError(e);}
};
$('package').onchange=async()=>{try {await peer.seed($('package').files);}catch(e){showError(e);}};
$('sync').onclick=async()=>{$('sync').disabled=true;try {await peer.sync();}catch(e){showError(e);}finally{$('sync').disabled=false;}};
$('forget').onclick=async()=>{try {await peer.forget();$('dataset').textContent='Local copy deleted.';$('files').replaceChildren();$('sync').disabled=true;}catch(e){showError(e);}};
