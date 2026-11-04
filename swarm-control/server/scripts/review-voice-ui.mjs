import {createServer} from 'node:http';
import {readFile,mkdir,writeFile} from 'node:fs/promises';
import {fileURLToPath} from 'node:url';
import {join} from 'node:path';
import {createRequire} from 'node:module';
import assert from 'node:assert/strict';
const {chromium,expect}=createRequire(new URL('../../package.json',import.meta.url))('@playwright/test');
const view=fileURLToPath(new URL('../view/',import.meta.url)),output=process.argv[2]||'/tmp/codex-voice-review';await mkdir(output,{recursive:true});
const id='11111111-1111-4111-8111-111111111111';let messages=[],uploads=[],failSpeech=false;
const files=new Set(['codex.js','codex-voice.js','codex-queue.js','codex-screenshots.js','codex.css','style.css']);
const server=createServer(async(req,res)=>{
 try{
  const path=new URL(req.url,'http://localhost').pathname;res.setHeader('Content-Type','application/json');
  if(path.endsWith('/transcribe')){const chunks=[];for await(const chunk of req)chunks.push(chunk);const audio=Buffer.concat(chunks);uploads.push({size:audio.length,type:req.headers['content-type']});if(failSpeech){res.statusCode=503;res.end(JSON.stringify({error:{message:'Synthetic unavailable Whisper'}}));return;}res.end(JSON.stringify({text:'Please review this draft.'}));return;}
  if(path.endsWith('/queue/cancel')){let raw='';for await(const c of req)raw+=c;const body=JSON.parse(raw);messages.find(m=>m.id===body.id).status='cancelled';res.end('{"accepted":true}');return;}
  if(path.endsWith('/queue')){if(req.method==='POST'){let raw='';for await(const c of req)raw+=c;const body=JSON.parse(raw);messages.push({id:body.request_id,text:body.text,status:'queued',created:Date.now()/1000,note:'Waiting for idle',thread_id:body.thread_id});res.end('{"accepted":true,"message":"Message queued"}');}else res.end(JSON.stringify({messages}));return;}
  if(path.endsWith('/threads')){res.end(JSON.stringify({threads:[{id,display_title:'Voice test thread',activity:'running',updated_at:Date.now()/1000,cwd:'/synthetic'}],connected:true,machine:'Fixture',next_offset:null}));return;}
  if(path.endsWith('/threads/'+id)){res.end(JSON.stringify({id,display_title:'Voice test thread',cwd:'/synthetic',owner_connected:true,activity:'running',messages:[]}));return;}
  if(path==='/'){res.setHeader('Content-Type','text/html');res.end('<!doctype html><html lang="en"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Voice review</title><link rel="stylesheet" href="/style.css"><link rel="stylesheet" href="/codex.css"><main><section id="codex-context"></section></main><script type="module" src="/codex.js"></script></html>');return;}
  if(files.has(path.slice(1))){res.setHeader('Content-Type',path.endsWith('.js')?'text/javascript':'text/css');res.end(await readFile(join(view,path.slice(1))));return;}
  res.writeHead(404).end('{}');
 }catch{res.writeHead(500).end('{}');}
});await new Promise(r=>server.listen(0,'127.0.0.1',r));const base=`http://127.0.0.1:${server.address().port}`;
const browser=await chromium.launch({executablePath:process.env.CHROME_PATH||'/usr/bin/google-chrome',headless:true,args:['--use-fake-ui-for-media-stream','--use-fake-device-for-media-stream']});
try{
 for(const width of [390,1440]){
  messages=[];uploads=[];failSpeech=false;const context=await browser.newContext({viewport:{width,height:1000},reducedMotion:'reduce'});const page=await context.newPage(),errors=[];page.on('pageerror',e=>errors.push(e.message));
  async function connect(){await page.locator('input[type=password]').fill('synthetic-token');await page.getByRole('button',{name:'Connect',exact:true}).click();await page.locator('.codex-thread').first().click();await page.locator('textarea').waitFor();}
  async function record(){await page.getByRole('button',{name:'Record voice message',exact:true}).click();await expect(page.locator('.codex-voice p')).toContainText('Recording ·');await page.waitForTimeout(1200);}
  await page.goto(base);await connect();await page.locator('textarea').fill('Existing draft.');await record();
  await page.getByRole('button',{name:'Stop · keep draft',exact:true}).click();await expect(page.locator('.codex-voice p')).toContainText('Draft ready');
  assert.equal(messages.length,0,'Stop must never submit');await expect(page.locator('textarea')).toHaveValue('Existing draft.\nPlease review this draft.');assert(uploads[0].size>32);assert.match(uploads[0].type,/audio\//);
  await page.reload();await connect();await expect(page.locator('textarea')).toHaveValue('Existing draft.\nPlease review this draft.');
  await record();await page.getByRole('button',{name:'Finish & send',exact:true}).click();await expect(page.locator('textarea')).toHaveValue('');await expect(page.locator('.codex-message-queue article')).toHaveCount(1);assert.equal(messages[0].thread_id,id);assert.match(messages[0].text,/Existing draft/);
  await page.getByRole('button',{name:'Cancel queued message',exact:true}).click();await expect(page.locator('.codex-message-queue article')).toHaveAttribute('data-status','cancelled');
  failSpeech=true;await page.locator('textarea').fill('Keep me');await record();await page.getByRole('button',{name:'Stop · keep draft',exact:true}).click();await expect(page.locator('.codex-voice p')).toContainText('Recording retained');await expect(page.locator('textarea')).toHaveValue('Keep me');assert.equal(messages.length,1);
  failSpeech=false;await page.getByRole('button',{name:'Retry transcription',exact:true}).click();await expect(page.locator('textarea')).toHaveValue('Keep me\nPlease review this draft.');assert.equal(messages.length,1,'Retry must keep draft');
  await page.screenshot({path:join(output,`voice-draft-${width}.png`),fullPage:true});
  await record();await page.getByRole('button',{name:'Lock',exact:true}).click();await expect(page.locator('.codex-workspace')).toBeHidden();assert.equal(messages.length,1);assert.equal(await page.evaluate(()=>Object.keys(sessionStorage).filter(k=>k.startsWith('swarm-codex-draft:')).length),0);
  await connect();await page.evaluate(()=>navigator.mediaDevices.getUserMedia=async()=>{throw new DOMException('Denied','NotAllowedError')});await page.getByRole('button',{name:'Record voice message',exact:true}).click();await expect(page.locator('.codex-voice p')).toContainText('Microphone unavailable');
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);assert.deepEqual(errors,[]);await context.close();
 }
 await writeFile(join(output,'report.json'),JSON.stringify({status:'passed',widths:[390,1440],checks:['real MediaRecorder capture','stop never sends','reload restores draft','finish queues once','queue cancellation','retry keeps draft','lock stops capture and clears draft','permission denied','no overflow or JS errors']}));console.log('Voice and queue browser checks passed.');
}finally{await browser.close();server.closeAllConnections();await new Promise(r=>server.close(r));}
