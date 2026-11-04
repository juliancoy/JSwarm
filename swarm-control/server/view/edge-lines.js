const valid=v=>Number.isFinite(v)&&v>=0;
const palettes={send:['#08080c','#780800','#e52b05','#ff9a14','#ffe37a','#fffbea'],receive:['#f04438','#ffe347','#22c96c']};
function heat(value,colors){const t=Math.max(0,Math.min(1,value))*(colors.length-1),i=Math.min(colors.length-2,Math.floor(t)),f=t-i;const rgb=s=>s.slice(1).match(/../g).map(v=>parseInt(v,16));const a=rgb(colors[i]),b=rgb(colors[i+1]);return `rgb(${a.map((v,j)=>Math.round(v+(b[j]-v)*f)).join(',')})`;}
export class EdgeLines{
 constructor(){this.history=new Map();this.scale=.001;}
 key(edge){return [edge.a,edge.b].sort().join('|');}
 update(edges,now=Date.now()){
  for(const edge of edges){
   const key=this.key(edge),r=this.history.get(key)||{points:[]};
   let forward=null,reverse=null,stamp=null;
   for(const flow of [edge,...edge.relationships||[]]){
    const time=Date.parse(flow.observed);if(!Number.isFinite(time)||now-time>10000)continue;
    const send=valid(flow.upload_mbps)?flow.upload_mbps:flow.kind==='outbound'&&valid(flow.actual_mbps)?flow.actual_mbps:null;
    const receive=valid(flow.download_mbps)?flow.download_mbps:null;
    const same=flow.a===key.split('|')[0];
    if(valid(same?send:receive))forward=(forward??0)+(same?send:receive);
    if(valid(same?receive:send))reverse=(reverse??0)+(same?receive:send);
    stamp=Math.max(stamp||0,time);
   }
   if(r.stamp!==stamp||!r.points.length){r.points.push({time:now,forward,reverse});r.stamp=stamp;}
   r.points=r.points.filter(p=>now-p.time<=120000);r.seen=now;r.forward=forward;r.reverse=reverse;
   for(const side of ['forward','reverse']){const values=r.points.map(p=>p[side]).filter(valid);r[side+'Mean']=values.length?values.reduce((a,b)=>a+b,0)/values.length:0;r[side+'Peak']=Math.max(0,...values);}
   this.history.set(key,r);
  }
  for(const [key,r]of this.history)if(now-r.seen>120000)this.history.delete(key);
  this.scale=Math.max(.001,...[...this.history.values()].flatMap(r=>[r.forwardMean,r.reverseMean]));
 }
 draw(ctx,edge,p,q,ra,rb,perspective){
  const key=this.key(edge),r=this.history.get(key)||{},flip=edge.b===perspective;
  const from=flip?q:p,to=flip?p:q,source=flip?edge.b:edge.a;
  const dx=to.x-from.x,dy=to.y-from.y,d=Math.hypot(dx,dy);if(d<1)return;
  const ux=dx/d,uy=dy/d,start=(flip?rb:ra)+8,end=(flip?ra:rb)+8,length=d-start-end;if(length<8)return;
  const now=Date.now(),points=(r.points||[]).filter(v=>now-v.time<=120000);
  const span=Math.min(120000,Math.max(10000,now-(points[0]?.time??now)));
  const point=(time,height)=>{const along=start+length*Math.max(0,Math.min(1,(time-now+span)/span));return {x:from.x+ux*along-uy*height,y:from.y+uy*along+ux*height};};
  for(const [side,sign]of [['send',-1],['receive',1]]){
   const forward=(source===key.split('|')[0])===(side==='send'),metric=forward?'forward':'reverse';
   const peak=Math.max(.001,r[metric+'Peak']||0),height=4+20*(r[metric+'Mean']||0)/this.scale;
   const known=points.some(v=>valid(v[metric]));
   const baseA=point(now-span,sign*2),baseB=point(now,sign*2);
   ctx.lineWidth=1;ctx.strokeStyle='#647583';ctx.setLineDash(known?[]:[3,5]);ctx.beginPath();ctx.moveTo(baseA.x,baseA.y);ctx.lineTo(baseB.x,baseB.y);ctx.stroke();ctx.setLineDash([]);
   for(let i=1;i<points.length;i++){
    const a=points[i-1],b=points[i];if(!valid(a[metric])||!valid(b[metric])||b.time-a.time>10000)continue;
    const av=Math.min(1,a[metric]/peak),bv=Math.min(1,b[metric]/peak);
    const a0=point(a.time,sign*2),b0=point(b.time,sign*2),a1=point(a.time,sign*(2+height*av)),b1=point(b.time,sign*(2+height*bv));
    const gradient=ctx.createLinearGradient(a0.x,a0.y,b0.x,b0.y);gradient.addColorStop(0,heat(av,palettes[side]));gradient.addColorStop(1,heat(bv,palettes[side]));ctx.fillStyle=gradient;
    ctx.beginPath();ctx.moveTo(a0.x,a0.y);ctx.lineTo(a1.x,a1.y);ctx.lineTo(b1.x,b1.y);ctx.lineTo(b0.x,b0.y);ctx.closePath();ctx.fill();
    ctx.strokeStyle=gradient;ctx.lineWidth=1.5;ctx.beginPath();ctx.moveTo(a1.x,a1.y);ctx.lineTo(b1.x,b1.y);ctx.stroke();
   }
  }
  ctx.lineWidth=1;
 }
}
