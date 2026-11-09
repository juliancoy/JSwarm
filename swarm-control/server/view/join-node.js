const id=new URLSearchParams(location.search).get('request'),message=document.getElementById('message'),approve=document.getElementById('approve'),signIn=document.getElementById('sign-in');
async function read(){
 if(!id||!/^[A-Za-z0-9_-]{43}$/.test(id))throw Error('Start JSwarm join on the computer you want to enroll.');
 const response=await fetch('/api/v1/swarm/node-enrollment?request='+encodeURIComponent(id),{cache:'no-store'});
 if(response.status===401){signIn.href='/auth/login?node_request='+encodeURIComponent(id);signIn.hidden=false;message.textContent='Sign in to review this computer.';return;}
 if(!response.ok)throw Error('This node sign-in expired or is unavailable. Start a new join on the computer.');
 const data=await response.json(),list=document.getElementById('identity');
 for(const [label,value] of [['Computer',data.name],['Machine fingerprint',data.machine],['Local address',data.address]]){const dt=document.createElement('dt'),dd=document.createElement('dd');dt.textContent=label;dd.textContent=value;list.append(dt,dd);}
 message.textContent=data.approved?'Approved. Return to the JSwarm command.':'Approve only if you started this join and the fingerprint matches.';approve.disabled=data.approved;
}
approve.onclick=async()=>{approve.disabled=true;try{const response=await fetch('/api/v1/swarm/node-enrollment',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({request_id:id})});if(!response.ok)throw Error('Approval failed. Sign in again or restart the join.');message.textContent='Approved. Return to the JSwarm command to finish joining.';}catch(error){message.textContent=error.message;approve.disabled=false;}};
read().catch(error=>{message.textContent=error.message;});
