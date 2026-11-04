const nonWebPorts=new Set([22,25,53,110,143,465,587,993,995,1433,2375,2376,3306,4369,5432,5433,5671,5672,6379,6380,11211,27017]);
export function containerWebsites(host,container){
 const endpoints=[],seen=new Set();
 for(const [port,bindings] of Object.entries(container.ports||{})){
  const [number,protocol]=port.split('/'),internal=Number(number);
  if(protocol!=='tcp'||nonWebPorts.has(internal))continue;
  for(const binding of bindings||[]){
   const published=Number(binding.HostPort);
   if(!Number.isInteger(published)||published<1||published>65535)continue;
   const address=binding.HostIp||'0.0.0.0',loopback=address==='::1'||address.startsWith('127.');
   const target=['0.0.0.0','::','::1'].includes(address)||loopback?host:address;
   const hostname=target.includes(':')?'['+target+']':target;
   const scheme=[443,8443].includes(internal)||[443,8443].includes(published)?'https':'http';
   const url=scheme+'://'+hostname+':'+published+'/';
   if(seen.has(url))continue;seen.add(url);
   endpoints.push({url,loopback,label:scheme.toUpperCase()+' · '+published+' → '+internal+(loopback?' · host-only':'')});
  }
 }
 return endpoints.sort((a,b)=>Number(a.loopback)-Number(b.loopback));
}
