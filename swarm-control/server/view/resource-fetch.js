// Poll independent coordinator snapshots; never turn a failed request into host data.
export async function readResourceSnapshots(read, previous={}) {
 const endpoints={network:'/api/devices?resolve=0',metrics:'/api/cpu',resources:'/api/resources'};
 const entries=Object.entries(endpoints);
 const results=await Promise.allSettled(entries.map(([,url])=>read(url)));
 const denied=results.find(r=>r.status==='rejected'&&r.reason?.status===401);
 if(denied)throw denied.reason;
 const snapshots={...previous},failed=[];
 results.forEach((result,i)=>{
  const key=entries[i][0];
  if(result.status==='fulfilled')snapshots[key]=result.value;
  else failed.push(key);
 });
 return {snapshots,failed};
}
