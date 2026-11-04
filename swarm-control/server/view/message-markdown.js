import {lexer} from './marked-vendor.js';
const node=(tag,text)=>{const el=document.createElement(tag);if(text!==undefined)el.textContent=text;return el;};
function textValue(value){
 const decoder=document.createElement('textarea');
 // Only character references are parsed; literal tag openers are escaped first.
 decoder.innerHTML=String(value).replaceAll('<','&lt;');
 return decoder.value;
}
function safeLink(value){
 try{
  const url=new URL(textValue(value),location.href);
  if(!['http:','https:','mailto:'].includes(url.protocol))return null;
  // Workspace file references cannot be opened through the public web server.
  if(/^\/(?:home|mnt|tmp|var|etc|Users)\//.test(value))return null;
  return url.href;
 }catch{return null;}
}
function inline(parent,tokens,depth=0){
 if(depth>40){parent.append(document.createTextNode(tokens.map(t=>t.raw||t.text||'').join('')));return;}
 for(const token of tokens||[]){
  let el;
  if(['strong','em','del'].includes(token.type))el=node(token.type);
  else if(token.type==='codespan'){parent.append(node('code',token.text));continue;}
  else if(token.type==='br'){parent.append(node('br'));continue;}
  else if(token.type==='link'||token.type==='image'){
   const href=safeLink(token.href);el=node(href?'a':'span');
   if(href){el.href=href;el.rel='noopener noreferrer';if(new URL(href).origin!==location.origin)el.target='_blank';}
   if(token.title)el.title=token.title;
   if(token.type==='image'){el.textContent='Image: '+(token.text||'View image');parent.append(el);continue;}
  }else if(token.tokens){el=node('span');}
  else{parent.append(document.createTextNode(token.type==='text'?textValue(token.text??''):(token.text??token.raw??'')));continue;}
  inline(el,token.tokens||[{type:'text',text:token.text}],depth+1);parent.append(el);
 }
}
function blocks(parent,tokens,depth=0){
 if(depth>40){parent.append(node('pre',tokens.map(t=>t.raw||t.text||'').join('')));return;}
 for(const token of tokens||[]){
  let el;
  switch(token.type){
   case 'space':continue;
   case 'heading':el=node('h'+Math.min(6,token.depth));inline(el,token.tokens);break;
   case 'paragraph':case 'text':el=node('p');inline(el,token.tokens||[{type:'text',text:token.text}]);break;
   case 'hr':el=node('hr');break;
   case 'blockquote':el=node('blockquote');blocks(el,token.tokens,depth+1);break;
   case 'code':{
    el=node('div');el.className='message-code';const toolbar=node('div');toolbar.className='message-code-toolbar';
    toolbar.append(node('span',token.lang||'Code'));const copy=node('button','Copy');copy.type='button';copy.setAttribute('aria-label','Copy code');
    copy.onclick=async()=>{try{await navigator.clipboard.writeText(token.text);copy.textContent='Copied';}catch{copy.textContent='Copy unavailable';}setTimeout(()=>copy.textContent='Copy',2000);};
    toolbar.append(copy);const pre=node('pre');pre.append(node('code',token.text));el.append(toolbar,pre);break;
   }
   case 'list':
    el=node(token.ordered?'ol':'ul');if(token.ordered)el.start=token.start;
    for(const item of token.items){const li=node('li');if(item.task){li.className='message-task';const check=node('input');check.type='checkbox';check.disabled=true;check.checked=item.checked;li.append(check);}
     for(const child of item.tokens){if(child.type==='checkbox')continue;if(!item.loose&&child.type==='text')inline(li,child.tokens||[{type:'text',text:child.text}]);else blocks(li,[child],depth+1);}el.append(li);}
    break;
   case 'table':{
    el=node('div');el.className='message-table';el.tabIndex=0;el.setAttribute('role','region');el.setAttribute('aria-label','Message table');const table=node('table'),thead=node('thead'),head=node('tr');
    token.header.forEach(cell=>{const th=node('th');th.scope='col';inline(th,cell.tokens);head.append(th);});thead.append(head);table.append(thead);
    const body=node('tbody');for(const row of token.rows){const tr=node('tr');for(const cell of row){const td=node('td');inline(td,cell.tokens);tr.append(td);}body.append(tr);}table.append(body);el.append(table);break;
   }
   default:el=node('p',token.raw??token.text??'');
  }
  parent.append(el);
 }
}
export function renderMessageMarkdown(text){
 const root=node('div');root.className='message-markdown';const source=String(text??'');
 // Large or unfinished responses remain readable even if parsing fails.
 try{if(source.length>200000)throw Error('Plain text fallback');blocks(root,lexer(source,{gfm:true,breaks:false}));}
 catch{root.replaceChildren(node('pre',source));}
 return root;
}
