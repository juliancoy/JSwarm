// Images stay in this composer until the authenticated instruction is submitted.
export function screenshotPicker(form, input, changed) {
  const root=document.createElement('div');root.className='codex-screenshots';
  const choose=document.createElement('button');choose.type='button';choose.textContent='Attach screenshots';
  const files=document.createElement('input');files.type='file';files.accept='image/png,image/jpeg,image/webp';files.multiple=true;files.hidden=true;files.setAttribute('aria-label','Choose screenshots');
  const hint=document.createElement('small');hint.textContent='Choose photos/screenshots, paste an image, or drop it here. Up to 4 images, 4 MB total after compression.';
  const previews=document.createElement('ul');previews.className='codex-screenshot-previews';previews.setAttribute('aria-label','Attached screenshots');
  const status=document.createElement('p');status.setAttribute('role','status');
  root.append(choose,files,hint,previews,status);
  let images=[],disabled=false,processing=false,epoch=0;
  const notify=(dirty=false)=>{choose.disabled=disabled||processing;changed(dirty);};
  function render(dirty=false){previews.replaceChildren();for(const image of images){const li=document.createElement('li'),img=document.createElement('img'),name=document.createElement('span'),remove=document.createElement('button');img.src=image.data_url;img.alt=image.name;name.textContent=image.name;remove.type='button';remove.textContent='Remove '+image.name;remove.disabled=disabled;remove.onclick=()=>{images=images.filter(item=>item!==image);render(true);};li.append(img,name,remove);previews.append(li)}notify(dirty);}
  async function add(selected){
    if(disabled||processing)return;processing=true;const active=epoch,startCount=images.length;status.textContent='Preparing screenshots…';notify();
    try{for(const file of selected){
      if(active!==epoch)return;
      if(images.length>=4)throw Error('You can attach up to 4 screenshots. Remove one before adding another.');
      if(!['image/png','image/jpeg','image/webp'].includes(file.type)||file.size>20*1024*1024)throw Error('Choose a PNG, JPEG or WebP image up to 20 MB.');
      const url=URL.createObjectURL(file),img=new Image();let data;
      try{img.src=url;await img.decode();if(img.naturalWidth*img.naturalHeight>40e6)throw Error('This image is too large. Crop it before attaching.');const scale=Math.min(1,4096/Math.max(img.naturalWidth,img.naturalHeight)),canvas=document.createElement('canvas');canvas.width=Math.max(1,Math.round(img.naturalWidth*scale));canvas.height=Math.max(1,Math.round(img.naturalHeight*scale));canvas.getContext('2d').drawImage(img,0,0,canvas.width,canvas.height);
        let blob=await new Promise(resolve=>canvas.toBlob(resolve,'image/png'));if(blob?.size>1024*1024)blob=await new Promise(resolve=>canvas.toBlob(resolve,'image/webp',.92));if(!blob||blob.size>2*1024*1024)throw Error('This screenshot is still over 2 MB after compression. Crop it and try again.');
        if(images.reduce((sum,item)=>sum+item.bytes,0)+blob.size>4*1024*1024)throw Error('Screenshots exceed 4 MB total. Remove an image or choose a smaller crop.');
        data=await new Promise((resolve,reject)=>{const reader=new FileReader();reader.onload=()=>resolve(reader.result);reader.onerror=()=>reject(Error('Could not read this screenshot.'));reader.readAsDataURL(blob)});
        if(active!==epoch)return;images.push({name:(file.name||'Screenshot').slice(0,120),data_url:data,bytes:blob.size});
      }finally{URL.revokeObjectURL(url)}
    }if(active===epoch)status.textContent=images.length+' screenshot'+(images.length===1?'':'s')+' ready to send.';
    }catch(error){if(active===epoch)status.textContent=error.message||'Could not read this image.';}finally{if(active===epoch){processing=false;render(images.length!==startCount);}}
  }
  choose.onclick=()=>files.click();files.onchange=()=>{void add([...files.files]);files.value='';};
  input.addEventListener('paste',event=>{const images=[...(event.clipboardData?.items||[])].filter(item=>item.type.startsWith('image/')).map(item=>item.getAsFile()).filter(Boolean);if(images.length){event.preventDefault();void add(images)}});
  form.addEventListener('dragover',event=>{if([...event.dataTransfer?.types||[]].includes('Files'))event.preventDefault()});
  form.addEventListener('drop',event=>{if(event.dataTransfer?.files.length){event.preventDefault();void add([...event.dataTransfer.files])}});
  return {root,get count(){return images.length},get processing(){return processing},snapshot:()=>images.map(({name,data_url})=>({name,data_url})),clear(){epoch++;images=[];processing=false;status.textContent='';render(true)},setDisabled(value){disabled=value;render()}};
}
