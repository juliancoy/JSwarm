const token=new URLSearchParams(location.hash.slice(1)).get('token');
history.replaceState(null,'',location.pathname);
if(token){
 const button=document.querySelector('#download');button.hidden=false;
 document.querySelector('#status').textContent='Download this invitation onto the device you want to register.';
 button.addEventListener('click',()=>{
  const file=new Blob([JSON.stringify({url:location.origin,token})],{type:'application/json'});
  const url=URL.createObjectURL(file),anchor=document.createElement('a');anchor.href=url;anchor.download='invitation.json';anchor.click();setTimeout(()=>URL.revokeObjectURL(url),1000);
 });
}
