// Keep the owner token in memory only; never send it over a plaintext LAN page.
let token='';
export function authorizedAction(path,options={}) {
  if(location.protocol!=='https:' && !['localhost','127.0.0.1','[::1]'].includes(location.hostname))
    throw Error('Use This device · swarm controls, or open the dashboard through a localhost SSH tunnel to control tests.');
  if(!token)token=prompt('Master API token (stored only until this page closes)')?.trim()||'';
  if(!token)throw Error('A master API token is required.');
  return fetch(path,{...options,headers:{...options.headers,Authorization:'Bearer '+token}}).then(response=>{if(response.status===401)token='';return response;});
}
