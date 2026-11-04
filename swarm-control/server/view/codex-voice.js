// Recordings stay in memory. Stop always leaves a draft; only an explicit send submits it.
export function voiceRecorder({input,transcribe,onDraft,onSend,onBusy}){
 const root=document.createElement('div');root.className='codex-voice';
 const button=(text,label)=>{const b=document.createElement('button');b.type='button';b.textContent=text;if(label)b.setAttribute('aria-label',label);root.append(b);return b;};
 const mic=button('🎙 Record','Record voice message'),stop=button('Stop · keep draft'),finish=button('Finish & send'),retry=button('Retry transcription');
 const status=document.createElement('p');status.setAttribute('role','status');root.append(status);
 let recorder=null,stream=null,chunks=[],audio=null,epoch=0,timer=null,controller=null,active=false,blocked=false,started=0;
 const supported=!!(globalThis.isSecureContext&&navigator.mediaDevices?.getUserMedia&&globalThis.MediaRecorder);
 function paint(){mic.disabled=blocked||active||!supported;stop.hidden=!recorder||recorder.state!=='recording';finish.hidden=stop.hidden;retry.hidden=!audio||active;}
 function tracks(){stream?.getTracks().forEach(t=>t.stop());stream=null;clearInterval(timer);timer=null;}
 function busy(value){active=value;onBusy(value);paint();}
 async function decode(sendAfter=false){
  if(!audio)return;const current=epoch;busy(true);status.textContent='Transcribing on CompAI…';controller=new AbortController();
  try{
   const result=await transcribe(audio,controller.signal);if(current!==epoch)return;
   if(!result.text?.trim()){status.textContent='No speech detected. Your draft is unchanged.';audio=null;return;}
   const next=[input.value.trim(),result.text.trim()].filter(Boolean).join('\n');
   if(next.length>input.maxLength){status.textContent='The transcript would exceed the message limit. Shorten the draft, then retry.';return;}
   input.value=next;audio=null;onDraft();status.textContent='Draft ready. Review or edit it, then send when you’re ready.';
   busy(false);if(sendAfter)onSend();
  }catch(error){if(current===epoch&&error.name!=='AbortError')status.textContent=error.message+' Recording retained here for retry.';}
  finally{if(current===epoch){controller=null;busy(false);paint();}}
 }
 function end(sendAfter){if(!recorder||recorder.state!=='recording')return;recorder.sendAfter=sendAfter;recorder.stop();tracks();paint();}
 mic.onclick=async()=>{
  if(active||blocked)return;const current=++epoch;busy(true);status.textContent='Allow microphone access to start recording.';
  try{
   const incoming=await navigator.mediaDevices.getUserMedia({audio:true});
   if(current!==epoch){incoming.getTracks().forEach(t=>t.stop());return;}
   stream=incoming;const mime=['audio/webm;codecs=opus','audio/ogg;codecs=opus','audio/mp4'].find(t=>MediaRecorder.isTypeSupported(t));
   recorder=new MediaRecorder(stream,mime?{mimeType:mime,audioBitsPerSecond:64000}:undefined);const rec=recorder;chunks=[];audio=null;let bytes=0;started=Date.now();
   rec.ondataavailable=event=>{if(current!==epoch||!event.data.size)return;chunks.push(event.data);bytes+=event.data.size;if(bytes>8*1024*1024)end(false);};
   rec.onerror=()=>{if(current!==epoch)return;rec.sendAfter=false;tracks();status.textContent='Recording interrupted. Your existing draft is safe.';if(rec.state!=='inactive')rec.stop();else busy(false);};
   rec.onstop=()=>{if(current!==epoch)return;tracks();audio=new Blob(chunks,{type:rec.mimeType});chunks=[];recorder=null;if(audio.size>8*1024*1024){audio=null;status.textContent='Recording exceeded 8 MB. Please record a shorter message.';busy(false);return;}void decode(rec.sendAfter===true);};
   rec.start(1000);status.textContent='Recording · 0s. Stop keeps a draft; Finish & send submits it.';paint();
   timer=setInterval(()=>{const seconds=Math.floor((Date.now()-started)/1000);status.textContent=`Recording · ${seconds}s / 180s. Stop keeps a draft.`;if(seconds>=180)end(false);},500);
  }catch(error){if(current===epoch){tracks();recorder=null;busy(false);status.textContent='Microphone unavailable. Check browser permission and use the HTTPS portal.';}}
 };
 stop.onclick=()=>end(false);finish.onclick=()=>end(true);retry.onclick=()=>void decode(false);
 function cancel(){epoch++;controller?.abort();controller=null;if(recorder?.state==='recording')recorder.stop();tracks();recorder=null;chunks=[];audio=null;busy(false);status.textContent='';}
 window.addEventListener('pagehide',cancel);
 if(!supported)status.textContent='Microphone recording requires HTTPS (or localhost) and a supported browser.';
 paint();return {root,cancel,get active(){return active;},get hasRecording(){return !!audio;},setDisabled(value){blocked=value;paint();}};
}
