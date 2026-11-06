import {renderMessageMarkdown} from './message-markdown.js';
import {voiceRecorder} from './codex-voice.js';
import {messageQueue} from './codex-queue.js';
import {screenshotPicker} from './codex-screenshots.js';
const root = document.querySelector('#codex-context');
const secureRemote=root.dataset.remote==='true' && location.protocol==='https:';
const allowedBrowser=secureRemote || ['localhost','127.0.0.1','[::1]'].includes(location.hostname);
const el = (tag, text, cls) => { const node = document.createElement(tag); if (text) node.textContent = text; if (cls) node.className = cls; return node; };
let token = '', selected = null, offset = 0, next = null, busy = false, generation = 0, renderedThread = '', renderedMessages = '', observedActivity = '';
root.classList.add('codex-app');
const heading = el('div', '', 'panel-heading');
const title = el('div'); title.append(el('h1', secureRemote ? 'Swarm Control' : 'Codex workspace'), el('p', secureRemote ? 'Codex on CompAI' : 'Codex on this machine', 'muted'));
const deviceStatus=el('p','','muted');title.append(deviceStatus);
const lock = el('button', 'Lock'); lock.hidden = true;
const mapLink=el('a','Swarm status ↗','codex-map-link'); mapLink.href='/swarm-status'; mapLink.target='_blank'; mapLink.rel='noopener noreferrer'; mapLink.title='Open remote swarm status';
const taskLink=el('a','Resource usage','codex-map-link');taskLink.href='/task-manager';
const researchLink=el('a','Research ↗','codex-map-link');researchLink.href='/research.html';
const historyLink=el('a','History','codex-map-link');historyLink.href='/history';
const pipelineLink=el('a','Pipeline','codex-map-link');pipelineLink.href='/pipeline.html';
const headerActions=el('div','','codex-header-actions'); headerActions.append(taskLink,pipelineLink,historyLink,mapLink,researchLink,lock);
const userBadge=el('div','','pidp-user');userBadge.hidden=true;const userAvatar=el('img');userAvatar.width=36;userAvatar.height=36;userAvatar.referrerPolicy='no-referrer';const userInitials=el('span','','pidp-user-initials');const userName=el('span','','pidp-user-name');userBadge.append(userAvatar,userInitials,userName);headerActions.append(userBadge); heading.append(title,headerActions);
const unlock = el('form', '', 'codex-unlock'); unlock.hidden=secureRemote;
const label = el('label', 'Codex owner token '), password = el('input'); password.type = 'password'; password.autocomplete = 'off'; password.required = true; password.placeholder = 'Enter your owner token'; label.append(password);
const connect = el('button', 'Connect'); connect.type = 'submit'; unlock.append(label, connect, el('small', secureRemote ? 'Sign in with PIdP on this device. Lock signs out this browser.' : 'Use localhost or an SSH tunnel. This separate owner token is never shared with test workers.'));
if(!allowedBrowser){password.disabled=true;connect.disabled=true;}
const deviceLabel=el('input');deviceLabel.value= /Android/i.test(navigator.userAgent)?'Android':/iPhone|iPad/i.test(navigator.userAgent)?'iOS': 'Browser';deviceLabel.maxLength=64;deviceLabel.required=true;
if(secureRemote){const deviceName=el('label','Device name ');deviceName.append(deviceLabel);const signIn=el('button','Sign in with PIdP');signIn.type='submit';unlock.replaceChildren(deviceName,signIn,el('p','Use your authorized PIdP account. Multiple devices can stay signed in. Lock signs out this browser only.'));}
const reauthenticate=el('a','Sign in again with PIdP','codex-reauth');reauthenticate.href='/auth/login';reauthenticate.target='_blank';reauthenticate.rel='noopener noreferrer';reauthenticate.title='Opens a new tab so your draft and attachments stay here';reauthenticate.hidden=true;
const status = el('p', secureRemote ? 'Checking sign-in…' : 'Connect to view your conversations.', 'codex-status'); status.setAttribute('role', 'status');
const retrySignIn=el('button','Retry connection');retrySignIn.type='button';retrySignIn.hidden=true;retrySignIn.onclick=()=>void restoreDevice();
if(secureRemote)root.setAttribute('aria-busy','true');
const workspace = el('div', '', 'codex-workspace'); workspace.hidden = true;
const sidebar = el('div', '', 'codex-sidebar'), list = el('div', '', 'codex-list'), controls = el('div', '', 'codex-pagination');
const searchLabel=el('label','Find a conversation','codex-search'), search=el('input'); search.type='search'; search.placeholder='Search this page of conversations'; searchLabel.append(search);
const noResults=el('p','No conversations match. Try another name or load an older page.','codex-no-results'); noResults.hidden=true; noResults.setAttribute('role','status');
const sidebarHeading=el('h2','Conversations'); sidebar.prepend(sidebarHeading,searchLabel);
list.setAttribute('aria-label','Conversations');
const previous = el('button', 'Newer'), more = el('button', 'Older'), refresh = el('button', 'Refresh'); controls.append(previous, more, refresh); sidebar.append(list, noResults, controls);
const newChat = el('button', 'New chat'); newChat.type = 'button';
const sidebarTitle=el('div','','codex-sidebar-heading'); sidebarTitle.append(sidebarHeading,newChat); sidebar.prepend(sidebarTitle);
const conversation = el('div', '', 'codex-conversation'), info = el('div'), transcript = el('div', '', 'codex-transcript'); transcript.tabIndex = 0; transcript.setAttribute('aria-label', 'Conversation messages');
const empty=el('div','','codex-empty'); empty.append(el('span','Your workspace, at a glance','codex-eyebrow'),el('h2','Choose a conversation'),el('p','Select a thread to read its history and send the next instruction. You can review any conversation before taking action.'));
const back=el('button','← Conversations','codex-back'); back.type='button'; back.onclick=()=>{if(history.state?.swarmConversation)history.back();else showConversations();};
const activity = el('div', '', 'codex-activity'); activity.hidden=true; activity.setAttribute('role','status'); activity.setAttribute('aria-live','polite');
const activityDot=el('span','','codex-activity-dot'); activityDot.setAttribute('aria-hidden','true');
const activityText=el('span'); activity.append(activityDot,activityText);
const latest=el('button','↓ Latest messages','codex-latest'); latest.type='button'; latest.hidden=true;
latest.onclick=()=>{transcript.scrollTop=transcript.scrollHeight;latest.hidden=true;};
transcript.onscroll=()=>{latest.hidden=transcript.scrollHeight-transcript.scrollTop-transcript.clientHeight<80;};
function showActivity(state,text){activity.hidden=false;activity.dataset.state=state;if(activityText.textContent!==text)activityText.textContent=text;}
const form = el('form', '', 'codex-compose'), promptLabel = el('label', '', 'codex-prompt'), input = el('textarea'); input.rows = 3; input.maxLength = 16000; input.required = false; input.placeholder = 'Ask Codex to do something…'; input.setAttribute('aria-label','Instruction to the selected thread'); input.setAttribute('aria-describedby','codex-compose-help'); promptLabel.append(input);
const actions = el('div', '', 'codex-actions'), mode = el('select'); mode.setAttribute('aria-label', 'Instruction delivery');
for (const [value, text] of [['send', 'Queue for next turn'], ['steer', 'Steer current turn']]) { const option = el('option', text); option.value = value; mode.append(option); }
const send = el('button', 'Send instruction'); send.type = 'submit'; send.disabled=true; const open = el('button', 'Retry connection'); open.type = 'button'; send.setAttribute('aria-label','Send instruction');send.title='Send instruction · Ctrl/⌘ + Enter';send.textContent='↑';actions.append(mode, send);open.className='codex-open'; const composeHelp=el('small', 'Stop keeps a draft in this browser tab. Send queues it for this thread; Steer interrupts the active turn. Ctrl/⌘ + Enter submits. Full-access conversations run without local command approval. Restricted conversations keep their approval settings.'); composeHelp.id='codex-compose-help'; const help=el('details','','codex-compose-help');help.append(el('summary','Sending options'),composeHelp);form.append(promptLabel, actions, help,open);open.hidden=true;
const screenshots=screenshotPicker(form,input,dirty=>{if(dirty)requestId=null;send.disabled=voiceBusy||busy||screenshots.processing||send.dataset.connected!=='true'||(!input.value.trim()&&!screenshots.count);});form.insertBefore(screenshots.root,actions);screenshots.root.querySelector('button').title='Choose screenshots, paste an image, or drop it here. Up to 4 images, 4 MB total.';
input.addEventListener('keydown',event=>{if(event.key==='Enter' && (event.ctrlKey||event.metaKey) && !event.isComposing){event.preventDefault();if(!send.disabled)form.requestSubmit();}});
form.hidden=true; transcript.hidden=true; info.className='codex-thread-info'; conversation.append(back,empty,info, transcript, latest, activity, form); workspace.append(sidebar, conversation); root.append(heading, unlock, status, retrySignIn, reauthenticate, workspace);
let requestId = null, voiceBusy=false;
const draftsPrefix='swarm-codex-draft:';
function saveDraft(){if(selected){try{if(input.value)sessionStorage.setItem(draftsPrefix+selected,input.value);else sessionStorage.removeItem(draftsPrefix+selected);}catch{status.textContent='Draft is in the composer; browser storage is unavailable.';}}}
function restoreDraft(){try{input.value=sessionStorage.getItem(draftsPrefix+selected)||'';}catch{input.value='';}}
const queue=messageQueue(api,()=>selected);conversation.insertBefore(queue.root,form);
const voice=voiceRecorder({input,onDraft:()=>{requestId=null;saveDraft();input.dispatchEvent(new Event('input'));},onSend:()=>form.requestSubmit(),onBusy:value=>{voiceBusy=value;input.readOnly=value||busy;send.disabled=value||busy||screenshots.processing||(!input.value.trim()&&!screenshots.count);},transcribe:async(audio,signal)=>{
 const response=await fetch('/api/v1/master/codex/transcribe',{method:'POST',headers:{...(!secureRemote?{Authorization:'Bearer '+token}:{}),'Content-Type':audio.type},body:audio,signal:AbortSignal.any([signal,AbortSignal.timeout(150000)])});
 const result=await response.json();if(!response.ok)throw Error(result.error?.message||'Transcription failed.');return result;
}});form.insertBefore(voice.root,actions);

// UUID generation also works on trusted LAN HTTP, where randomUUID is unavailable.
function uuid() { const b = crypto.getRandomValues(new Uint8Array(16)); b[6] = (b[6] & 15) | 64; b[8] = (b[8] & 63) | 128; return [...b].map((x, i) => ([4,6,8,10].includes(i) ? '-' : '') + x.toString(16).padStart(2, '0')).join(''); }
async function api(path, body) {
  if(!allowedBrowser)throw Error('Open this page through localhost or an SSH tunnel before entering a Codex owner token.');
  const response = await fetch('/api/v1/master/codex/' + path, { method: body ? 'POST' : 'GET', cache: 'no-store', headers: { ...(!secureRemote?{Authorization:'Bearer '+token}:{}), ...(body ? { 'Content-Type': 'application/json' } : {}) }, ...(body ? { body: JSON.stringify(body) } : {}), signal: AbortSignal.timeout(20000) });
  const value = await response.json();
  if (!response.ok) {if(secureRemote&&response.status===401)reauthenticate.hidden=false;throw Object.assign(new Error(value.error?.message || 'Codex connection failed'),{status:response.status});}
  if(secureRemote)reauthenticate.hidden=true;
  return value;
}
function failure(error) { if(error.status===401&&secureRemote){reauthenticate.hidden=false;status.textContent='Sign-in expired or access denied. Your unsent draft is kept; sign in again in a new tab.';status.dataset.state='error';if(selected)showActivity('authentication_required','Sign in again to send. Your draft stays here.');return;}if(selected)showActivity('unknown','Could not refresh or confirm delivery. Check the connection and refresh.'); status.textContent = error.message; status.dataset.state='error'; }
function filterThreads(){const term=search.value.trim().toLocaleLowerCase();const nodes=[...list.querySelectorAll('.codex-thread')]; for(const node of nodes)node.hidden=!node.textContent.toLocaleLowerCase().includes(term); noResults.hidden=!term||!nodes.length||nodes.some(node=>!node.hidden);}
search.oninput=filterThreads;
async function load() {
  const epoch = generation;
  const value = await api('threads?offset=' + offset);
  if (epoch !== generation) return;
  next = value.next_offset; previous.disabled = offset === 0; more.disabled = next === null;
  list.replaceChildren();
  for (const thread of value.threads) {
    const button = el('button', '', 'codex-thread'); button.type = 'button'; button.setAttribute('aria-pressed', String(selected === thread.id));
    button.append(el('strong', thread.display_title || 'Untitled conversation'), el('span', (thread.activity === 'running' ? '● Generating response' : thread.activity === 'idle' ? 'Past turn completed' : 'State unknown') + ' · ' + new Date(thread.updated_at * 1000).toLocaleString()), el('small', thread.cwd));
    button.dataset.activity=thread.activity; button.dataset.updated=new Date(thread.updated_at * 1000).toLocaleString(); button.onclick = () => choose(thread.id).catch(failure); list.append(button);
  }
  if (!value.threads.length) list.append(el('p', 'No local conversations found.'));
  filterThreads();
  status.dataset.state=value.connected?'connected':'disconnected';
  status.textContent = value.machine + (value.connected ? ' · Conversation service available' : ' · VS Code disconnected');
  if(secureRemote){const r=await fetch('/api/v1/swarm/session',{cache:'no-store'});if(r.ok){const d=await r.json();deviceStatus.textContent=d?`This controller: ${d.label} · Last seen ${new Date(d.last_seen*1000).toLocaleString()}`:'';const name=d.user?.name||'PIdP user';userName.textContent=name;userBadge.title='Signed in with PIdP as '+name;userBadge.hidden=false;userInitials.textContent=name.split(/\s+/).slice(0,2).map(part=>part[0]).join('').toUpperCase();userAvatar.hidden=true;userInitials.hidden=false;userAvatar.onload=()=>{userAvatar.hidden=false;userInitials.hidden=true;};userAvatar.onerror=()=>{userAvatar.hidden=true;userInitials.hidden=false;};userAvatar.alt=name+' avatar';if(d.user?.avatar_url&&userAvatar.getAttribute('src')!==d.user.avatar_url)userAvatar.src=d.user.avatar_url;else if(d.user?.avatar_url&&userAvatar.complete&&userAvatar.naturalWidth){userAvatar.hidden=false;userInitials.hidden=true;}}}
}
function showConversations(){saveDraft();empty.hidden=false;workspace.classList.remove('codex-reading');list.querySelector('[aria-pressed=true]')?.focus();}
let navigationInitialized=false;
function initializeNavigation(){
 if(navigationInitialized)return;navigationInitialized=true;
 const id=new URLSearchParams(location.hash.slice(1)).get('conversation');
 const valid=id&&/^[a-f0-9-]{36}$/i.test(id);
 if(valid&&history.state?.swarmConversation===id){void choose(id,{recordHistory:false}).catch(failure);return;}
 history.replaceState({swarmConversation:null},'',location.pathname+location.search);
 if(valid)void choose(id).catch(failure);
}
window.addEventListener('popstate',()=>{
 if(!navigationInitialized)return;
 const id=history.state?.swarmConversation;
 if(!id){showConversations();return;}
 if(selected===id){workspace.classList.add('codex-reading');empty.hidden=true;void readThread().catch(failure);void queue.refresh();return;}
 void choose(id,{recordHistory:false}).catch(failure);
});
async function choose(id,{recordHistory=true}={}) {
  if (busy || voiceBusy) return;
  saveDraft();
  if (selected !== id && (screenshots.count||screenshots.processing||voice.hasRecording) && !window.confirm('Discard unsent attachments and switch threads? Your text draft is saved.')) return;
  if (selected !== id) { voice.cancel();screenshots.clear();input.value = ''; requestId = null; renderedThread=''; renderedMessages=''; observedActivity=''; }
  if(recordHistory&&history.state?.swarmConversation!==id)history.pushState({swarmConversation:id},'',location.pathname+location.search+'#conversation='+encodeURIComponent(id));
  activity.hidden=true; latest.hidden=true;
  workspace.classList.add('codex-reading'); conversation.setAttribute('aria-busy','true');
  selected = id;restoreDraft();queue.clear();
  send.disabled = true;
  try { await readThread(); await load(); await queue.refresh(); } finally { conversation.setAttribute('aria-busy','false'); }
}
const autoConnectAt=new Map();
let threadReadRevision=0;
async function readThread() {
  if (!selected) { form.hidden = true; return; }
  const id = selected, epoch = generation, revision = ++threadReadRevision;
  const thread = await api('threads/' + encodeURIComponent(id));
  if (epoch !== generation || selected !== id || revision !== threadReadRevision) return;
  empty.hidden=workspace.classList.contains('codex-reading'); transcript.hidden=false;
  const connectionState=thread.connection_state || (thread.owner_connected?'connected':'detached');
  if(!thread.owner_connected && ['detached','connecting'].includes(connectionState) && Date.now()-(autoConnectAt.get(id)||0)>10000){
    if(autoConnectAt.size>=100)autoConnectAt.delete(autoConnectAt.keys().next().value);
    autoConnectAt.set(id,Date.now());
    void api('commands',{thread_id:id,action:'connect'}).catch(()=>{});
  }
  const metadata=JSON.stringify([id,thread.display_title,thread.cwd,thread.model,thread.owner_connected,connectionState,thread.ide_context,thread.history_limited]);
  if(renderedThread!==metadata){
  info.replaceChildren(el('h2', thread.display_title || 'Untitled conversation'), el('p', thread.cwd + ' · ' + (thread.model || 'Thread model'), 'muted'));
  const owner = el('p', thread.owner_connected ? 'Conversation connected' : connectionState==='unavailable' ? 'Local Codex is unavailable. Your queued messages are saved.' : connectionState==='needs_attention' ? 'Could not connect this conversation. Retry connection below.' : 'Connecting this conversation automatically. You can queue your message now.'); info.append(owner);
  if (thread.ide_context) { const details = el('details'), summary = el('summary', 'VS Code context'); details.append(summary, el('pre', typeof thread.ide_context === 'string' ? thread.ide_context : JSON.stringify(thread.ide_context, null, 2))); info.append(details); }
  if (thread.history_limited) info.append(el('small', 'Showing the latest 60 messages from the recent history window.'));
  renderedThread=metadata;
  }
  const messageSignature=JSON.stringify(thread.messages);
  const nearBottom = !renderedMessages || transcript.scrollHeight - transcript.scrollTop - transcript.clientHeight < 80;
  if(renderedMessages!==messageSignature){
  transcript.replaceChildren();
  for (const message of thread.messages) { const item = el('article', '', 'codex-message ' + message.role); item.append(el('strong', message.role === 'user' ? 'You' : 'Codex'), renderMessageMarkdown(message.text)); transcript.append(item); }
  if (!thread.messages.length) transcript.append(el('p', 'No message text is available in the recent history.'));
  renderedMessages=messageSignature;
  if (nearBottom) transcript.scrollTop = transcript.scrollHeight;
  latest.hidden=nearBottom;
  }
  form.hidden = false; send.disabled = voiceBusy || busy || screenshots.processing || (!input.value.trim()&&!screenshots.count);
  if(!busy)showActivity(!thread.owner_connected?'waiting':thread.activity, !thread.owner_connected ? connectionState==='needs_attention' ? 'Conversation connection needs attention. Your messages are saved.' : connectionState==='unavailable' ? 'Waiting for local Codex to reconnect. Your messages are saved.' : 'Connecting conversation… Queued messages will send automatically.' : thread.activity === 'running' ? 'Codex is working. Updates every 5 seconds.' : thread.activity === 'idle' ? 'Codex is ready for your next instruction.' : 'Generation status unavailable. Your queued messages remain saved.');
  transcript.setAttribute('aria-busy',String(thread.activity==='running'));
  for(const button of list.querySelectorAll('.codex-thread'))if(button.getAttribute('aria-pressed')==='true'){button.dataset.activity=thread.activity;button.querySelector('span').textContent=(thread.activity==='running'?'● Generating response':thread.activity==='idle'?'Past turn completed':'State unknown')+' · '+button.dataset.updated;}
  send.dataset.connected='true';
  if(observedActivity!==thread.activity){mode.value = 'send'; observedActivity=thread.activity;}
  info.dataset.connected=String(thread.owner_connected);
  open.disabled=busy;open.hidden=thread.owner_connected || ['connecting','detached'].includes(connectionState);
}
unlock.onsubmit = async event => {
  event.preventDefault(); if(secureRemote){location.assign('/auth/login?device_label='+encodeURIComponent(deviceLabel.value.trim()));return;} token = password.value.trim(); connect.disabled = true;
  try {
    await load(); password.value = ''; unlock.hidden = true; workspace.hidden = false; lock.hidden = false; form.hidden = !selected; initializeNavigation(); }
  catch (error) { token = ''; failure(error); } finally { connect.disabled = false; }
};
lock.onclick = async () => { voice.cancel();queue.clear();screenshots.clear();for(const key of Object.keys(sessionStorage))if(key.startsWith(draftsPrefix))sessionStorage.removeItem(key); if(secureRemote){const response=await fetch('/logout',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});if(!response.ok){failure(Error('Could not revoke this device. Try Lock again.'));return;}} userBadge.hidden=true;userAvatar.removeAttribute('src');generation++; token = ''; selected = null; input.value = ''; requestId = null; list.replaceChildren(); transcript.replaceChildren(); info.replaceChildren(); workspace.hidden = true; workspace.classList.remove('codex-reading'); empty.hidden=false; transcript.hidden=true; form.hidden=true; renderedThread=''; renderedMessages=''; observedActivity=''; search.value=''; activity.hidden=true; latest.hidden=true; lock.hidden = true; unlock.hidden = false; status.dataset.state=''; status.textContent = 'Locked'; };
refresh.onclick = () => { load().then(readThread).catch(failure); };
let newChatRequest = null;
newChat.onclick = async () => {
  if (busy || voiceBusy) return;
  saveDraft();
  if ((input.value||screenshots.count||screenshots.processing||voice.hasRecording) && !window.confirm('Discard the unsent instruction and start a new chat?')) return;
  busy = true; newChat.disabled = true; lock.disabled = true; send.disabled = true;
  newChatRequest ||= uuid();
  status.textContent = 'Creating conversation…';
  try {
    const result = await api('commands', { action: 'new', request_id: newChatRequest });
    status.textContent = result.message;
    if (result.accepted) {
      voice.cancel();selected = result.thread_id; offset = 0; screenshots.clear();input.value = ''; requestId = null; newChatRequest = null;
      renderedThread = ''; renderedMessages = ''; observedActivity = ''; search.value = '';
      workspace.classList.add('codex-reading');
      await load(); await readThread(); input.focus(); status.textContent = result.message;
    }
  } catch (error) { failure(new Error(error.message + ' Refresh before trying again; the chat may already exist.')); }
  finally { busy = false; newChat.disabled = false; lock.disabled = false; await readThread().catch(failure); }
};
previous.onclick = () => { offset = Math.max(0, offset - 20); load().catch(failure); };
more.onclick = () => { if (next !== null) { offset = next; load().catch(failure); } };
input.oninput = () => { requestId = null;saveDraft(); send.disabled=voiceBusy || busy || send.dataset.connected!=='true' || screenshots.processing || (!input.value.trim()&&!screenshots.count); };
form.onsubmit = async event => {
  event.preventDefault(); if (voiceBusy || busy || screenshots.processing || !selected || (!input.value.trim()&&!screenshots.count)) return;
  saveDraft();busy = true;voice.setDisabled(true); screenshots.setDisabled(true);send.disabled = true; lock.disabled = true; input.readOnly=true; mode.disabled=true; send.textContent='…';send.setAttribute('aria-label','Sending…'); showActivity('sending','Sending your instruction…'); requestId ||= uuid();
  try {
    const payload={thread_id:selected,text:input.value,request_id:requestId,images:screenshots.snapshot()};
    const result = await api(mode.value==='send'?'queue':'commands',mode.value==='send'?payload:{...payload,action:'steer'});
    status.textContent = result.message;
    if (result.accepted) { screenshots.clear();input.value = '';saveDraft(); requestId = null;await queue.refresh(); }
  } catch (error) { failure(error.status===401?error:new Error(error.message + ' Check the conversation before retrying; delivery may be uncertain.')); }
  finally { busy = false;voice.setDisabled(false); screenshots.setDisabled(false);lock.disabled = false; input.readOnly=false; mode.disabled=false; send.textContent='↑';send.setAttribute('aria-label','Send instruction'); await readThread().catch(failure); }
};
open.onclick = async () => { if (!selected || busy) return; open.disabled = true; try { const result = await api('commands', { thread_id: selected, action: 'open' }); status.textContent = result.message; } catch (error) { failure(error); } finally { open.disabled = false; } };
async function restoreDevice(){
  if(!secureRemote)return;
  if(new URLSearchParams(location.hash.slice(1)).has('pair')){history.replaceState(null,'',location.pathname);status.textContent='Pairing links are retired. Sign in with PIdP.';}
  unlock.hidden=true;retrySignIn.hidden=true;reauthenticate.hidden=true;
  root.setAttribute('aria-busy','true');status.dataset.state='';status.textContent='Checking sign-in…';
  try{await load();unlock.hidden=true;workspace.hidden=false;lock.hidden=false;form.hidden=!selected;initializeNavigation();}
  catch(error){
    userBadge.hidden=true;reauthenticate.hidden=true;
    if(error.status===401){unlock.hidden=false;status.textContent='Sign in to view your conversations.';status.dataset.state='';}
    else{status.textContent='Could not check sign-in. Check your connection and retry.';status.dataset.state='error';retrySignIn.hidden=false;}
  }finally{root.setAttribute('aria-busy','false');}

}
void restoreDevice();
let polling = false;
setInterval(async () => { if ((!token && !secureRemote) || workspace.hidden || !workspace.classList.contains('codex-reading') || document.hidden || busy || polling) return; polling = true; try { if (selected) {await readThread();await queue.refresh();} } catch (error) { failure(error); } finally { polling = false; } }, 5000);

// Refresh immediately after returning from another app or unlocking the phone.
function refreshVisibleThread(){if(!document.hidden&&!workspace.hidden&&selected&&!busy)void readThread().then(()=>queue.refresh()).catch(failure);}
document.addEventListener('visibilitychange',refreshVisibleThread);
window.addEventListener('online',refreshVisibleThread);
