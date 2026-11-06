const nav=document.getElementById('node-web-navigation');
if(nav){
 try{
  const response=await fetch('/api/jswarm-web',{cache:'no-store',signal:AbortSignal.timeout(5000)});
  if(response.ok){
   const context=await response.json();
   const label=document.createElement('strong');label.textContent='Served by '+context.node.replace(/\.local$/,'');
   const links=context.nodes.map(node=>{
    const link=document.createElement('a');link.textContent=node.name.replace(/\.local$/,'');
    link.href=location.hostname.endsWith('.localhost')?node.url:node.direct_url;
    if(node.name===context.node)link.setAttribute('aria-current','page');
    return link;
   });
   nav.replaceChildren(label,...links);nav.hidden=false;
   document.title='Pipeline · '+context.node.replace(/\.local$/,'');
  }
 }catch{/* The shared map page also works without a node web frontend. */}
}
