import test from 'node:test';
import assert from 'node:assert/strict';
import {networkView, edgeStyle, nodeVisual, trafficParticles} from '../view/model.js';

const host = {mac:'aa:00', ip:'10.42.0.1', self:true, ports:[22]};
const laptop = {mac:'bb:00', ip:'10.42.0.110', ports:[22]};
const cast = {mac:'cc:00', ip:'10.42.0.194', ports:[]};
const ap = {mac:'dd:00', ip:'10.42.0.233', ports:[80]};
const now = new Date().toISOString();
const base = {
  viewer:{id:host.mac, ip:host.ip, recognized:true}, devices:[host,laptop,cast,ap], interface:'enp7s0',
  router:{mac:ap.mac,ip:ap.ip,observed:now,clients:[{...host,wireless:false},{...laptop,wireless:true,tx_link_mbps:72.2,rx_link_mbps:52},{...cast,wireless:true}]},
  links:{enp7s0:{capacity_mbps:1000,capacity_basis:'Negotiated Ethernet'},wlan:{capacity_mbps:200}},
  interface_traffic:{enp7s0:{rx_mbps:2,tx_mbps:3,updated:now},wlan:{rx_mbps:5,tx_mbps:1,updated:now}},
  link_traffic:{updated:now,peers:{[laptop.mac]:{rx_mbps:1,tx_mbps:2}}},
  perspectives:{
    [host.mac]:{status:'measured',hostname:'host',interface:'enp7s0',gateway_interface:'wlan',gateway:'172.20.20.1',devices:[laptop,cast,ap],traffic:{rx_mbps:20},route:{hops:[{ttl:1,ip:'172.20.20.1',rtt_ms:2},{ttl:2,ip:null},{ttl:3,ip:'1.1.1.1',rtt_ms:10}]}},
    [laptop.mac]:{status:'measured',hostname:'laptop',gateway:host.ip,devices:[host,cast,ap],traffic:{rx_mbps:2},link:{capacity_mbps:72.2},route:{hops:[{ttl:1,ip:host.ip,rtt_ms:1},{ttl:2,ip:'172.20.20.1',rtt_ms:4},{ttl:3,ip:null},{ttl:4,ip:'1.1.1.1',rtt_ms:14}]}},
    [cast.mac]:{status:'unavailable',error:'No SSH login'}
  }
};
const pair = (view,a,b) => view.edges.find(e=>[e.a,e.b].includes(a)&&[e.a,e.b].includes(b));

test('starts from the visitor rather than the collector',()=>{
 const v=networkView({...base,viewer:{id:laptop.mac,ip:laptop.ip}});assert.equal(v.id,laptop.mac);assert.equal(v.source.you,true);assert.equal(v.observation.traffic.rx_mbps,2);
});
test('physical topology uses ASUS association rather than a neighbor-table star',()=>{
 const v=networkView(base,laptop.mac);assert.ok(pair(v,laptop.mac,ap.mac));assert.ok(pair(v,host.mac,ap.mac));assert.equal(pair(v,laptop.mac,host.mac),undefined);assert.equal(v.nodes.find(n=>n.id===host.mac).distance,2);assert.equal(v.nodes.find(n=>n.id===ap.mac).ip_hop,undefined);
});
test('IP hops omit the Layer 2 bridge and retain unknown TTL gaps',()=>{
 const v=networkView(base,laptop.mac,'ip');assert.ok(pair(v,laptop.mac,host.mac));assert.ok(v.nodes.some(n=>n.gap&&n.id.endsWith(':3')));assert.ok(v.edges.every(e=>e.a!==ap.mac&&e.b!==ap.mac));assert.equal(v.nodes.find(n=>n.id===host.mac).ip_hop,1);
});
test('duplicate route and association evidence creates one edge per unordered pair',()=>{
 const v=networkView(base);const pairs=v.edges.map(e=>[e.a,e.b].sort().join('|'));assert.equal(new Set(pairs).size,pairs.length);
});
test('PHY rate and observed gateway traffic remain distinct',()=>{
 const e=pair(networkView(base),laptop.mac,ap.mac);assert.equal(e.capacity_mbps,72.2);assert.equal(e.actual_mbps,3);assert.match(e.traffic_basis,/peer-to-peer/);const wire=pair(networkView(base),host.mac,ap.mac);assert.equal(wire.capacity_mbps,1000);assert.ok(edgeStyle(wire).outer>edgeStyle(e).outer);assert.ok(edgeStyle(e).inner>0);
});
test('unknown transit capacity never inherits the gateways traffic',()=>{
 const v=networkView(base,host.mac,'ip');const e=v.edges.find(e=>e.b==='gap:'+host.mac+':2');assert.equal(e.capacity_mbps,null);assert.equal(e.actual_mbps,null);assert.equal(edgeStyle(e).inner,0);
});
test('stale packet counters are unknown rather than a zero measurement',()=>{
 const data=structuredClone(base);data.link_traffic.updated='2000-01-01T00:00:00Z';assert.equal(pair(networkView(data),laptop.mac,ap.mac).actual_mbps,null);
});
test('unavailable device views use infrastructure evidence without inventing device telemetry',()=>{
 const v=networkView(base,cast.mac);assert.equal(v.measured,false);assert.equal(v.observation.traffic,undefined);assert.ok(pair(v,cast.mac,ap.mac));assert.equal(pair(v,cast.mac,ap.mac).capacity_mbps,null);
});
test('unidentified visitors have their own unmeasured perspective',()=>{
 const v=networkView({...base,viewer:{id:'ip:10.42.0.99',ip:'10.42.0.99',recognized:false}});assert.equal(v.source.ip,'10.42.0.99');assert.equal(v.measured,false);assert.deepEqual(v.edges,[]);
});

test('a measured device hostname takes precedence over a router vendor name',()=>{
 const v=networkView(base,laptop.mac);assert.equal(v.source.hostname,'laptop');
});
test('edge fill color changes with utilization and empty measurements remain empty',()=>{
 assert.notEqual(edgeStyle({capacity_mbps:100,actual_mbps:10}).color,edgeStyle({capacity_mbps:100,actual_mbps:90}).color);
 assert.equal(edgeStyle({capacity_mbps:100,actual_mbps:null}).inner,0);
 assert.equal(edgeStyle({capacity_mbps:100,actual_mbps:0}).inner,0);
});

test('default map extends beyond the upstream gateway and retains unknown hops',()=>{
 const v=networkView(base);
 assert.ok(v.nodes.some(n=>n.ip==='1.1.1.1'));
 const gap=v.nodes.find(n=>n.id==='gap:'+host.mac+':2');
 assert.ok(gap?.gap);
 assert.ok(pair(v,gap.id,'gateway:1.1.1.1'));
 assert.equal(pair(v,gap.id,'gateway:1.1.1.1').capacity_mbps,null);
 assert.equal(pair(v,gap.id,'gateway:1.1.1.1').actual_mbps,null);
});
test('LAN membership does not claim a direct physical cable',()=>{
 const e=pair(networkView(base),host.mac,ap.mac);
 assert.match(e.evidence,/intermediate switches are unknown/);
});

const outgoing = (source=laptop.mac,destination='8.8.8.8',bytes=1200) => ({source_mac:source,source_ip:'10.42.0.110',destination,bytes,mbps:.1,first_seen:now,last_seen:now,interface:'enp7s0',basis:'LAN ingress before NAT'});
const withOutbound = rows => ({...base,link_traffic:{...base.link_traffic,outbound:{available:true,scope:'Gateway only; switched traffic unknown',relationships:rows}}});
test('outbound destination relationships retain real infrastructure and source attribution',()=>{
 const v=networkView(withOutbound([outgoing()]),laptop.mac);
 assert.ok(pair(v,laptop.mac,ap.mac));
 const e=pair(v,laptop.mac,'external:8.8.8.8');
 assert.equal(e.kind,'outbound');assert.equal(e.bytes,1200);assert.equal(e.capacity_mbps,null);
 assert.match(e.evidence,/not a direct physical link/);
 assert.equal(pair(v,host.mac,'external:8.8.8.8'),undefined);
});
test('external selection remains a passive perspective and exposes its actual source',()=>{
 const v=networkView(withOutbound([outgoing()]),'external:8.8.8.8');
 assert.equal(v.id,'external:8.8.8.8');assert.equal(v.source.external,true);
 assert.deepEqual(v.source.ports,[]);assert.equal(v.observation,undefined);assert.equal(v.measured,false);
 assert.ok(pair(v,laptop.mac,v.id));assert.ok(pair(v,laptop.mac,ap.mac));
});
test('IP perspectives include outbound relationships without calling them IP hops',()=>{
 const v=networkView(withOutbound([outgoing()]),laptop.mac,'ip');
 assert.ok(pair(v,laptop.mac,'external:8.8.8.8'));
 assert.equal(v.nodes.find(n=>n.external).ip_hop,undefined);
});
test('idle, stale and unavailable capture destinations disappear instead of claiming zero traffic',()=>{
 let data=withOutbound([{...outgoing(),last_seen:'2000-01-01T00:00:00Z'}]);
 assert.equal(networkView(data).nodes.some(n=>n.external),false);
 data=withOutbound([outgoing()]);data.link_traffic.updated='2000-01-01T00:00:00Z';
 assert.equal(networkView(data).nodes.some(n=>n.external),false);
 data=withOutbound([outgoing()]);data.link_traffic.capture_error='stale config';
 assert.equal(networkView(data).nodes.some(n=>n.external),false);
 assert.equal(pair(networkView(data),laptop.mac,ap.mac).actual_mbps,null);
});
test('bounded destination display prioritizes selected sources and keeps selected destinations',()=>{
 const rows=Array.from({length:150},(_,i)=>outgoing(laptop.mac,'8.1.1.'+(i+1),i+1));
 rows.push(outgoing(cast.mac,'9.9.9.9',1));
 let v=networkView(withOutbound(rows),cast.mac);
 assert.ok(v.nodes.some(n=>n.id==='external:9.9.9.9'));
 assert.ok(v.nodes.filter(n=>n.external).length<=75);
 v=networkView(withOutbound(rows),'external:8.1.1.1');
 assert.equal(v.id,'external:8.1.1.1');
 assert.ok(v.nodes.filter(n=>n.external).length<=75);
});
test('passive sources absent from discovery are not attributed to the gateway',()=>{
 const v=networkView(withOutbound([outgoing('ee:00')]),'ee:00');
 assert.equal(v.source.passive,true);assert.equal(v.observation,undefined);
 assert.ok(pair(v,'ee:00','external:8.8.8.8'));
 assert.equal(pair(v,host.mac,'external:8.8.8.8'),undefined);
});
test('router counters never inflate gateway traffic and stale PHY data becomes unknown',()=>{
 const data=structuredClone(base);data.router.telemetry={interfaces:{WIRELESS0:{rx_mbps:900}}};
 assert.equal(pair(networkView(data),laptop.mac,ap.mac).actual_mbps,3);
 data.router.observed='2000-01-01T00:00:00Z';
 assert.equal(pair(networkView(data),laptop.mac,ap.mac).capacity_mbps,null);
});
test('relationship cap bounds canvas load while preserving a low-volume selected destination',()=>{
 const rows=Array.from({length:400},(_,i)=>outgoing('unknown:'+i,'8.8.8.8',i+100));
 rows.push(outgoing(cast.mac,'9.9.9.9',1));
 const v=networkView(withOutbound(rows),'external:9.9.9.9');
 assert.equal(v.id,'external:9.9.9.9');
 assert.ok(v.edges.filter(e=>e.kind==='outbound').length<=200);
 assert.ok(pair(v,cast.mac,'external:9.9.9.9'));
});

test('route and passive destination IPs use one selectable node',()=>{
 const v=networkView(withOutbound([outgoing(laptop.mac,'1.1.1.1')]),'external:1.1.1.1');
 assert.equal(v.nodes.filter(n=>n.ip==='1.1.1.1').length,1);
 assert.equal(v.id,'gateway:1.1.1.1');
 assert.ok(pair(v,laptop.mac,v.id));
});
test('repeated capture rows do not double count volume or relationships',()=>{
 const row=outgoing();
 const v=networkView(withOutbound([row,{...row}]),laptop.mac);
 assert.equal(v.source.traffic_summary.bytes,1200);
 assert.equal(v.source.traffic_summary.pairs,1);
 assert.equal(v.edges.filter(e=>e.kind==='outbound').length,1);
});
test('a traffic relationship preserves the capacity and evidence of an existing route edge',()=>{
 const v=networkView(withOutbound([outgoing(host.mac,'172.20.20.1')]),host.mac);
 const e=pair(v,host.mac,'gateway:172.20.20.1');
 assert.equal(e.capacity_mbps,200);
 assert.equal(e.kind,'route');
 assert.equal(e.relationships[0].kind,'outbound');
 assert.equal(e.evidence_sources.length,2);
});
test('duplicate inventory records with one IP retain services and known addresses',()=>{
 const data=structuredClone(base);
 data.devices.push({id:'ip:'+laptop.ip,ip:laptop.ip,ports:[80]});
 const v=networkView(data,laptop.mac);
 assert.equal(v.nodes.filter(n=>n.ip===laptop.ip).length,1);
 assert.deepEqual(v.source.ports,[22,80]);
 assert.ok(v.source.addresses.includes(laptop.ip));
});
test('different MACs sharing an IP remain separate devices',()=>{
 const data=structuredClone(base);
 data.devices.push({mac:'ee:00',ip:laptop.ip,ports:[]});
 const v=networkView(data);
 assert.equal(v.nodes.filter(n=>n.ip===laptop.ip).length,2);
});


test('traffic size is logarithmic, bounded and independent of selection',()=>{
 const small=nodeVisual({traffic_summary:{bytes:1024}});
 const large=nodeVisual({traffic_summary:{bytes:1024*1024*1024}});
 assert.ok(large.radius>small.radius);
 assert.ok(large.radius<=30);
 assert.equal(nodeVisual({perspective:true}).radius,nodeVisual({}).radius);
});
test('health distinguishes fresh probes, failed tests and unknown stale evidence',()=>{
 assert.equal(nodeVisual({}, {status:'measured',observed:now,internet_probe:{success:true}}).health,'healthy');
 assert.equal(nodeVisual({}, {status:'measured',observed:now,internet_probe:{success:false}}).health,'degraded');
 assert.equal(nodeVisual({test:{status:'failed',checked:now}}).health,'failed');
 assert.equal(nodeVisual({test:{status:'failed',checked:'2000-01-01'}}).health,'unknown');
 assert.equal(nodeVisual({association:{observed:now}}).health,'unknown');
});
test('particles require fresh directional traffic, including merged relationships',()=>{
 const flow={a:'source',b:'destination',kind:'outbound',actual_mbps:1,observed:now};
 assert.equal(trafficParticles(flow).length,1);
 assert.equal(trafficParticles({...flow,observed:'2000-01-01'}).length,0);
 assert.equal(trafficParticles({...flow,actual_mbps:0}).length,0);
 assert.equal(trafficParticles({...flow,kind:'ethernet'}).length,0);
 assert.equal(trafficParticles({kind:'route',relationships:[flow]})[0].a,'source');
});

test('geographic positions distinguish shared sites, public endpoints and unknown hops',async()=>{
 const {geographicNodes}=await import('../view/geography.js');
 const location={latitude:39,longitude:-76};
 const nodes=geographicNodes([{id:'local',mac:'aa:00'},{id:'second',mac:'bb:00'},{id:'remote',ip:'8.8.8.8',external:true},{id:'unknown',ip:'172.20.20.1',context:true}],{locations:{site:location,'8.8.8.8':{latitude:10,longitude:20}}});
 assert.equal(nodes[0].geographic.shared_site,true);
 assert.deepEqual(nodes[0].geographic.anchor,nodes[1].geographic.anchor);
 assert.equal(nodes[0].x,nodes[1].x);
 assert.notDeepEqual(nodes[0].geographic.offset,nodes[1].geographic.offset);
 assert.equal(nodes[2].x,80);
 assert.equal(nodes[2].geographic.shared_site,false);
 assert.equal(nodes[3].geographic,null);
 assert.equal(nodes[3].mapVisible,false);
});
test('missing and invalid locations are never mapped to zero latitude/longitude',async()=>{
 const {coordinates,geographicNodes}=await import('../view/geography.js');
 assert.equal(coordinates({latitude:null,longitude:null}),null);
 assert.equal(coordinates({latitude:99,longitude:0}),null);
 assert.equal(geographicNodes([{mac:'aa',id:'a'}])[0].geographic,null);
 assert.ok(coordinates({latitude:0,longitude:0}));
});

test('remote network metadata follows the IP without fabricating a website hostname',async()=>{
 const {label}=await import('../view/model.js');
 const data=withOutbound([outgoing()]);data.remote_ips={'8.8.8.8':{organization:'Example network',asn:123,ptr:null}};
 const node=networkView(data).nodes.find(n=>n.ip==='8.8.8.8');
 assert.equal(node.remote_info.asn,123);
 assert.equal(label(node),'Example network · 8.8.8.8');
 node.remote_info.ptr='dns.example.test';assert.equal(label(node),'dns.example.test');
});

test('website graph deduplicates reverse initiators without inventing service dependencies',async()=>{
 const {graph,changes}=await import('../view/website-model.js');
 const origins=[{origin:'https://codecollective.us'},{origin:'https://a.example'},{origin:'https://b.example'}];
 const summary={origins,requests:3,bytes:10,links:[{source:origins[0].origin,target:origins[1].origin,requests:2},{source:origins[1].origin,target:origins[0].origin,requests:1}]};
 const result=graph({site:'https://codecollective.us/',summary});
 assert.equal(result.edges.length,2);
 assert.equal(result.edges.filter(e=>!e.context).length,1);
 assert.equal(result.edges.find(e=>!e.context).requests,3);
 assert.equal(result.edges.find(e=>e.context).b,'https://b.example');
 const delta=changes(summary,{origins:[origins[0]],requests:1,bytes:5});
 assert.equal(delta.added.length,2);assert.equal(delta.requests,2);
 assert.equal(changes(summary,null),null);
});

test('provider families use exact DNS suffixes or known ASNs, never substring guesses',async()=>{
 const {provider}=await import('../view/providers.js');
 assert.equal(provider({ptr:'edge.facebook.com'}).id,'provider:facebook');
 assert.equal(provider({asn:13335}).id,'provider:cloudflare');
 assert.equal(provider({},'fonts.gstatic.com').id,'provider:google');
 assert.equal(provider({},'google.com.attacker.test'),null);
 assert.equal(provider({},'notgoogle.com'),null);
});
test('provider grouping preserves LAN devices, totals and individual endpoint evidence',async()=>{
 const {groupNetwork}=await import('../view/providers.js');
 const data=withOutbound([outgoing(laptop.mac,'8.8.8.8',100),outgoing(laptop.mac,'8.8.4.4',200)]);
 data.remote_ips={'8.8.8.8':{asn:15169},'8.8.4.4':{asn:15169}};
 const raw=networkView(data,laptop.mac),view=groupNetwork(raw),google=view.nodes.find(n=>n.id==='provider:google');
 assert.equal(google.members.length,2);assert.equal(google.traffic_summary.bytes,300);
 assert.ok(view.nodes.some(n=>n.id===laptop.mac));
 const edge=view.edges.find(e=>e.b==='provider:google');assert.equal(edge.bytes,300);assert.equal(edge.members.length,2);assert.equal(edge.capacity_mbps,null);
 assert.equal(edge.relationships.length,2);
 assert.equal(new Set(view.edges.map(e=>[e.a,e.b].sort().join('|'))).size,view.edges.length);
});
test('website groups combine explicit domain families and retain source origins',async()=>{
 const {groupWebsiteGraph}=await import('../view/providers.js');
 const node=id=>({id,origin:id,requests:1,bytes:10,statuses:{200:1},types:{script:1},ips:[]});
 const result=groupWebsiteGraph({root:'https://lifetech.fyi',nodes:[node('https://lifetech.fyi'),node('https://fonts.googleapis.com'),node('https://lh3.googleusercontent.com')],edges:[]});
 const google=result.nodes.find(n=>n.id==='provider:google');assert.equal(google.requests,2);assert.equal(google.bytes,20);assert.equal(google.members.length,2);
 assert.equal(result.root,'https://lifetech.fyi');
});

test('map settings persist and malformed or unavailable browser storage remains safe',async()=>{
 const {loadSettings,saveSettings,defaults}=await import('../view/settings.js');
 let value=null;const storage={getItem:()=>value,setItem:(_key,v)=>{value=v;}};
 const settings={...defaults,geographic:true,groupProviders:true,labels:true,paused:true,resolveNames:false,layer:'ip',repulsion:72};
 assert.equal(saveSettings(settings,storage),true);assert.deepEqual(loadSettings(storage),settings);
 value='{"geographic":"true","repulsion":999,"layer":"bad"}';
 assert.equal(loadSettings(storage).geographic,false);assert.equal(loadSettings(storage).repulsion,100);assert.equal(loadSettings(storage).layer,'links');
 value='{broken';assert.deepEqual(loadSettings(storage),defaults);
 assert.equal(saveSettings(settings,{setItem:()=>{throw Error('blocked');}}),false);
});


test('geographic clusters never change geographic coordinates and never connect unlocated nodes',async()=>{
 const {geographicNodes,geographicEdge}=await import('../view/geography.js');
 const input=Array.from({length:75},(_,i)=>({id:'node:'+i,ip:'8.1.1.'+i}));
 const locations=Object.fromEntries(input.map(node=>[node.ip,{latitude:39,longitude:-76}]));
 const mapped=geographicNodes([...input,{id:'unknown',ip:'10.0.0.1'}],{locations});
 for(const node of mapped.filter(n=>n.geographic)){
  assert.equal(node.x,-304);assert.equal(node.y,-156);
  assert.ok(Math.hypot(node.geographic.offset.x,node.geographic.offset.y)<=36.000001);
 }
 const indexed=new Map(mapped.map(node=>[node.id,node]));
 assert.equal(geographicEdge({a:input[0].id,b:'unknown'},indexed),false);
 assert.equal(geographicEdge({a:input[0].id,b:input[1].id},indexed),true);
 const reordered=geographicNodes(input.toReversed(),{locations});
 assert.deepEqual(mapped[4].geographic.offset,reordered.find(node=>node.id===mapped[4].id).geographic.offset);
});


test('geographic collision packing separates complete node rings at close and distant zooms',async()=>{
 const {geographicNodes,separateGeographicNodes}=await import('../view/geography.js');
 const nodes=Array.from({length:90},(_,i)=>({id:'node:'+i,ip:'8.1.1.'+i}));
 const locations=Object.fromEntries(nodes.map(node=>[node.ip,{latitude:39,longitude:-76}]));
 const mapped=geographicNodes(nodes,{locations});
 for(const zoom of [.1,.5,3,16,64]){
  const positions=[...separateGeographicNodes(mapped,zoom,()=>23).values()];
  for(let i=0;i<positions.length;i++)for(let j=i+1;j<positions.length;j++){
   assert.ok(Math.hypot(positions[i].x-positions[j].x,positions[i].y-positions[j].y)>=49-1e-6);
  }
 }
 assert.equal(mapped[0].x,-304);
});
