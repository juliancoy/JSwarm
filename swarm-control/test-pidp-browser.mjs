// Synthetic local controller sessions; actual PIdP protocol tests run separately.
import {spawn,execFileSync} from 'node:child_process';
import {mkdtemp,mkdir,rm} from 'node:fs/promises';
import {tmpdir} from 'node:os';import {join,resolve} from 'node:path';import assert from 'node:assert/strict';
import {chromium} from '@playwright/test';
const root=resolve(import.meta.dirname,'server'),temp=await mkdtemp(join(tmpdir(),'swarm-pidp-')),out=resolve(import.meta.dirname,'../artifacts/swarm-pidp');await mkdir(out,{recursive:true});
execFileSync('openssl',['req','-x509','-newkey','rsa:2048','-nodes','-days','1','-subj','/CN=localhost','-keyout',join(temp,'key'),'-out',join(temp,'cert')],{stdio:'ignore'});
const source=`import sys,os,json,ssl,time
sys.path.insert(0,${JSON.stringify(root)})
from api import Handler
import identity,devices,codex,message_queue
from http.server import ThreadingHTTPServer
from urllib.parse import urlsplit
state={'connected':True,'activity':'idle','recoverable':False};sent=[];connections=[]
queue=message_queue.MessageQueue();message_queue.manager=lambda:queue
codex.thread=lambda ident:{'id':ident}
def detail(ident):
 return {'display_title':'Navigation fixture','cwd':'/fixture','model':'Fixture','owner_connected':state['connected'],'connection_state':'connected' if state['connected'] else 'detached' if state['recoverable'] else 'unavailable','activity':state['activity'],'messages':[{'role':'assistant','text':'**Navigation** test response'}]+[{'role':'user','text':row['text']} for row in sent]}
def mutate(body):
 if body['action'] in ('connect','open'):
  connections.append(body['thread_id']);state['connected']=True;return {'accepted':True,'status':'connecting'}
 if not state['connected']:raise codex.ContextError('Fixture machine unavailable')
 if (body['action']=='send' and state['activity']!='idle') or (body['action']=='steer' and state['activity']!='running'):raise codex.ContextError('Fixture state changed')
 sent.append(body);state['activity']='running';return {'accepted':True,'message':'Fixture delivered'}
codex.detail=detail;codex.mutate=mutate
codex.rest=lambda method,path,query,body:(200,{'threads':[{'id':'11111111-1111-4111-8111-111111111111','display_title':'Navigation fixture','cwd':'/fixture','activity':state['activity'],'updated_at':1}],'next_offset':None,'machine':'Fixture','connected':True} if path.endswith('/threads') else mutate(body) if path.endswith('/commands') else detail('fixture'))
class Fixture(Handler):
 def do_POST(self):
  if self.path=='/fixture-expire':
   with devices.state() as value:
    value['sessions'][devices.digest(self.device())]['expires']=time.time()-1;devices.save(value)
   return self.reply(200,{'ok':True})
  if self.path=='/fixture-state':
   values=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
   assert set(values)<=set(state)
   state.update(values);return self.reply(200,{'ok':True})
  if self.path=='/fixture-tick':queue.tick();return self.reply(200,{'ok':True})
  return super().do_POST()
 def do_GET(self):
  path=urlsplit(self.path).path
  if path=='/fixture-metrics':return self.reply(200,{'sent':len(sent),'actions':[row['action'] for row in sent],'connections':len(connections)})
  if path=='/fixture-authorize':return self.reply(303,{},location='/auth/callback?state=fixture&code=fixture&iss=https%3A%2F%2Fidentity.example')
  if path=='/fixture-avatar.svg':return self.reply(200,b'<svg xmlns="http://www.w3.org/2000/svg" width="40" height="40"><rect width="40" height="40" fill="#6c9"/></svg>','image/svg+xml')
  return super().do_GET()
def login(origin,current=None,label='Browser'):
 return origin+'/fixture-authorize','__Host-compai_login=fixture; Path=/; Secure; HttpOnly; SameSite=Lax'
def complete(*args):
 with devices.state() as value:return devices.new_session(value,'Test browser',pidp_identity='synthetic-owner',pidp_subject='synthetic-owner')
def profile(origin,token):
 row=devices.describe(token);return {k:row[k] for k in ('label','created','last_seen','expires')}|{'auth_method':'pidp','user':{'name':'Synthetic Owner','avatar_url':origin+'/fixture-avatar.svg'}}
identity.start=login;identity.complete=complete;identity.session_info=profile;identity.config=lambda origin:{'owner_canonical_user_id':'synthetic-owner','allowed_subjects':['synthetic-owner']}
server=ThreadingHTTPServer(('127.0.0.1',0),Fixture);ctx=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER);ctx.load_cert_chain(${JSON.stringify(join(temp,'cert'))},${JSON.stringify(join(temp,'key'))});server.socket=ctx.wrap_socket(server.socket,server_side=True);server.public_origin='https://localhost:'+str(server.server_address[1]);print(server.public_origin,flush=True);server.serve_forever()`;
const proc=spawn('python3',['-u','-c',source],{env:{...process.env,MAP_CODEX_DEVICE_FILE:join(temp,'devices.json'),SWARM_STATE_DIR:join(temp,'state')},stdio:['ignore','pipe','pipe']});let browser;let stderr='';proc.stderr.on('data',d=>stderr+=d);
try{
 const origin=await new Promise((resolve,reject)=>{let line='';proc.stdout.on('data',data=>{line+=data;if(line.includes('\n'))resolve(line.trim())});proc.once('exit',()=>reject(Error(stderr)));});
 browser=await chromium.launch({headless:true,executablePath:'/usr/bin/google-chrome'});
 const contexts=[];
 for(const width of [1440,390]){
  const context=await browser.newContext({ignoreHTTPSErrors:true,viewport:{width,height:width===390?740:950}});contexts.push(context);const page=await context.newPage();const errors=[];page.on('pageerror',e=>{errors.push(e.message);console.error(e.message)});page.on('response',r=>{if(r.status()>=400)console.error(r.status(),r.url())});
  await page.goto(origin);await page.getByRole('button',{name:'Sign in with PIdP'}).click();await page.locator('.pidp-user').waitFor({timeout:15000}).catch(async e=>{console.error(await page.locator('body').innerText(),stderr);throw e;});await page.waitForFunction(()=>document.querySelector('.pidp-user img')?.naturalWidth>0);
  assert.equal(await page.locator('.pidp-user-name').textContent(),'Synthetic Owner');assert.equal(await page.getByPlaceholder('Enter your owner token').count(),0);assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
  assert.equal(await page.evaluate(async()=> (await fetch('/pair',{method:'POST',headers:{'Content-Type':'application/json'},body:'{"token":"obsolete-pairing-token"}'})).status),404);
  await page.locator('.codex-thread').first().click();await page.locator('.codex-thread-info h2').waitFor();
  const boxes=await page.evaluate(()=>{const rect=selector=>{const r=document.querySelector(selector).getBoundingClientRect();return {top:r.top,bottom:r.bottom}};return {send:rect('.codex-actions button'),nav:rect('.codex-header-actions'),height:innerHeight}});
  assert.ok(boxes.nav.top>=0);if(width===390)assert.ok(boxes.send.bottom<=boxes.height,JSON.stringify(boxes));
  await page.screenshot({path:join(out,`conversation-${width}.png`)});
  const draft=page.getByRole('textbox',{name:'Instruction to the selected thread'});await draft.fill('Keep my unsent draft');
  await page.evaluate(()=>history.back());await page.waitForFunction(()=>!document.querySelector('.codex-workspace').classList.contains('codex-reading'));
  assert.equal(new URL(page.url()).origin,origin);assert.equal(await page.locator('.codex-thread').first().isVisible(),true);
  await page.evaluate(()=>history.forward());await page.waitForFunction(()=>document.querySelector('.codex-workspace').classList.contains('codex-reading'));
  assert.equal(await draft.inputValue(),'Keep my unsent draft');
  await page.reload();await page.waitForFunction(()=>document.querySelector('.codex-workspace')?.classList.contains('codex-reading'));assert.equal(await draft.inputValue(),'Keep my unsent draft');
  if(width===390){await page.getByRole('button',{name:'← Conversations',exact:true}).click();await page.waitForFunction(()=>!document.querySelector('.codex-workspace').classList.contains('codex-reading'));}
  console.log('Back/Forward, reload and draft preservation checks:',width);
  const fixture=async(path,body)=>page.evaluate(async({path,body})=>{const response=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body||{})});if(!response.ok)throw Error('Fixture request failed');return response.json();},{path,body});
  const metrics=()=>page.evaluate(async()=> (await fetch('/fixture-metrics')).json());const baseline=(await metrics()).sent;
  await fixture('/fixture-state',{connected:false,activity:'idle'});
  if(width===390)await page.locator('.codex-thread').first().click();
  else {await page.evaluate(()=>history.back());await page.waitForFunction(()=>!document.querySelector('.codex-workspace').classList.contains('codex-reading'));await page.locator('.codex-thread').first().click();}
  await page.getByText('Waiting for local Codex to reconnect.',{exact:false}).waitFor();
  await draft.fill('Queued while waiting for machine confirmation');await page.getByRole('button',{name:'Send instruction',exact:true}).click();await page.locator('[data-status=queued]').waitFor();
  await fixture('/fixture-tick');assert.equal((await metrics()).sent,baseline);
  await fixture('/fixture-state',{connected:true,activity:'running'});await fixture('/fixture-tick');assert.equal((await metrics()).sent,baseline);
  await fixture('/fixture-state',{connected:true,activity:'idle'});await fixture('/fixture-tick');await fixture('/fixture-tick');assert.equal((await metrics()).sent,baseline+1);
  await page.locator('[data-status=queued]').waitFor({state:'detached',timeout:12000});
  await draft.fill('Send now through the real queue');await page.getByRole('button',{name:'Send instruction',exact:true}).click();await page.getByRole('button',{name:'Send now',exact:true}).click();
  await page.locator('[data-status=queued]').waitFor({state:'detached'});await fixture('/fixture-tick');const result=await metrics();assert.equal(result.sent,baseline+2);assert.equal(result.actions.at(-1),'steer');
  console.log('Real HTTP queue, machine wait, reconnect, exactly-once delivery and Send now:',width);
  await fixture('/fixture-state',{connected:false,recoverable:true,activity:'idle'});
  const beforeReconnect=(await metrics()).sent;await page.reload();
  await page.getByText('Conversation connected',{exact:true}).waitFor({timeout:12000});
  assert.ok((await metrics()).connections>=1);assert.equal((await metrics()).sent,beforeReconnect);
  await fixture('/fixture-state',{recoverable:false});
  console.log('Selected conversation reconnects automatically without starting a turn:',width);
  await draft.fill('Preserve this expired-session draft');const sentBeforeExpiry=(await metrics()).sent;await fixture('/fixture-expire');await page.getByRole('button',{name:'Send instruction',exact:true}).click();
  await page.getByRole('link',{name:'Sign in again with PIdP',exact:true}).waitFor();assert.equal(await draft.inputValue(),'Preserve this expired-session draft');assert.equal((await metrics()).sent,sentBeforeExpiry);
  const popupReady=context.waitForEvent('page');await page.getByRole('link',{name:'Sign in again with PIdP',exact:true}).click();const popup=await popupReady;await popup.locator('.pidp-user').waitFor();await popup.close();await page.bringToFront();await page.getByRole('link',{name:'Sign in again with PIdP',exact:true}).waitFor({state:'hidden',timeout:12000});assert.equal(await draft.inputValue(),'Preserve this expired-session draft');
  console.log('Expired-session rejection and new-tab sign-in preserve the unsent draft:',width);

  await page.screenshot({path:join(out,`pidp-${width}.png`)});assert.deepEqual(errors,[]);console.log('PIdP sign-in UI, avatar, retired pairing and responsive checks:',width);
 }
 const first=contexts[0].pages()[0],second=contexts[1].pages()[0];await first.getByRole('button',{name:'Lock',exact:true}).click();
 assert.equal(await second.evaluate(async()=> (await fetch('/api/v1/master/codex/threads')).status),200);
 assert.equal(await first.evaluate(async()=> (await fetch('/api/v1/master/codex/threads',{headers:{Authorization:'Bearer obsolete-owner-token'}})).status),401);
 console.log('Independent devices and retired bearer access verified');
}finally{await browser?.close();proc.kill('SIGTERM');await rm(temp,{recursive:true,force:true});}
