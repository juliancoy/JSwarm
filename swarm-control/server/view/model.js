export const key = device => (device.mac || device.ip).toLowerCase();
export const label = device => device.remote_info?.ptr || (device.external && device.remote_info?.organization ? device.remote_info.organization+' · '+device.ip : null) || device.title || device.hostname || device.router_name || (device.self ? 'Internet gateway' : device.ports?.includes(22) ? 'SSH device' : 'Unnamed device');
const total = value => value ? Math.max(0, value.rx_mbps || 0) + Math.max(0, value.tx_mbps || 0) : null;
const fresh = (value, seconds = 10) => {
  const age = Date.now() - Date.parse(value?.updated || value?.observed);
  return Number.isFinite(age) && age >= -5000 && age < seconds * 1000;
};

export function edgeStyle(edge) {
  const capacity = edge.capacity_mbps > 0 ? edge.capacity_mbps : null;
  const actual = Number.isFinite(edge.actual_mbps) ? Math.max(0, edge.actual_mbps) : null;
  const outer = capacity ? Math.min(18, 3 + 3 * Math.log10(1 + capacity)) : 3;
  const ratio = capacity && actual !== null ? Math.min(1, actual / capacity) : null;
  return {outer, inner: actual > 0 ? ratio === null ? 1.5 : Math.max(1.5, (outer - 2) * Math.sqrt(ratio)) : 0,
    color: ratio === null || ratio < .5 ? '#65d8ef' : ratio < .8 ? '#f0be70' : '#f57e70'};
}

// Visual encodings stay independent: size is observed volume, fill is role, ring is probe health.
export function nodeVisual(node, observation) {
  const bytes = Math.max(0, node.traffic_summary?.bytes || 0);
  const radius = 11 + Math.min(19, 3 * Math.log10(1 + bytes / 1024));
  let health = 'unknown', reason = 'No current health probe', ring = '#71818d';
  if (observation?.status === 'measured' && fresh(observation, 360) && typeof observation.internet_probe?.success === 'boolean') {
    health = observation.internet_probe.success ? 'healthy' : 'degraded';
    reason = observation.internet_probe.success ? 'Internet ping replied' : 'Internet ping did not reply; device is reachable';
  } else if (fresh({observed:node.test?.checked}, 360)) {
    if (node.test.status === 'passed') { health = 'healthy'; reason = 'Web test passed'; }
    if (node.test.status === 'partial') { health = 'degraded'; reason = 'Web test partially completed'; }
    if (node.test.status === 'failed') { health = 'failed'; reason = 'Web test failed; this does not establish that the device is offline'; }
  }
  ring = {healthy:'#73d5a0',degraded:'#f0be70',failed:'#f57e70',unknown:'#71818d'}[health];
  return {radius, health, reason, ring, size_basis:node.traffic_summary?.basis||'Retained outbound IP bytes observed at the gateway; logarithmic scale'};
}

export function trafficParticles(edge) {
  return [edge, ...(edge.relationships || [])].flatMap(flow=>{
    if(!fresh(flow))return [];
    if(flow.kind==='observed-connection')return [
      {a:flow.a,b:flow.b,actual_mbps:flow.upload_mbps},
      {a:flow.b,b:flow.a,actual_mbps:flow.download_mbps}
    ].filter(f=>f.actual_mbps>0);
    return flow.kind==='outbound'&&flow.actual_mbps>0?[flow]:[];
  });
}

// Association tables describe L2 attachment; TTL probes describe IP routing hops.
export function networkView(data, requested, layer = 'links', allConnections = false) {
  const inventory = new Map();
  function add(device) {
    if (device.mac) device = {...device, mac:device.mac.toLowerCase()};
    const existing = [...inventory.values()].find(item => device.ip && item.ip === device.ip && (!device.mac || !item.mac || device.mac === item.mac));
    const id = existing?.id || device.id || key(device), previous = inventory.get(id);
    inventory.set(id, {...previous, ...device, id, hostname: device.hostname || previous?.hostname,
      addresses: [...new Set([...(previous?.addresses || []), device.ip, device.ipv6].filter(Boolean))],
      ports: [...new Set([...(previous?.ports || []), ...(device.ports || [])])]});
    return id;
  }
  const byIP = ip => [...inventory.values()].find(device => device.ip === ip);
  function address(ip, title) {
    return byIP(ip)?.id || add({id: 'gateway:' + ip, ip, title, context: true, ports: []});
  }
  for (const observation of Object.values(data.perspectives || {})) for (const device of observation.devices || []) add({...device, self: false});
  for (const device of data.devices || []) add(device);
  for (const [observer, record] of Object.entries(data.perspectives || {})) {
    const device = inventory.get(observer);
    if (device && record.status === 'measured' && record.hostname && !device.hostname) add({...device, hostname:record.hostname});
  }
  const router = data.router;
  let ap;
  if (router) {
    ap = add({mac: router.mac, ip: router.ip, title: 'ASUS access point', context: true});
    for (const client of router.clients || []) {
      if (!client.mac || !client.ip || client.ip === 'offline') continue;
      const previous = inventory.get(client.mac);
      add({...previous, mac: client.mac, ip: client.ip, router_name: client.name, association: {...client,observed:router.observed}});
    }
  }
  for (const observation of Object.values(data.perspectives || {})) {
    if (observation.gateway) address(observation.gateway, 'Upstream gateway');
    for (const hop of observation.route?.hops || []) if (hop.ip) address(hop.ip, 'Route hop');
  }
  if (!inventory.has(data.viewer.id)) add({id: data.viewer.id, ip: data.viewer.ip, title: 'Your device', ports: []});
  const captured = fresh(data.link_traffic) && !data.link_traffic.capture_error ? data.link_traffic : null;
  const outbound = captured?.outbound;
  const uniqueFlows = new Map();
  for (const row of outbound?.available ? outbound.relationships || [] : []) {
    if (!row.source_mac || !row.destination || Date.now()-Date.parse(row.last_seen) >= 300000) continue;
    const normalized = {...row, source_mac:row.source_mac.toLowerCase()};
    const identity = normalized.source_mac+'|'+row.destination;
    const previous = uniqueFlows.get(identity);
    if (!previous || Date.parse(row.last_seen) >= Date.parse(previous.last_seen)) uniqueFlows.set(identity, normalized);
  }
  if(allConnections&&captured?.flows){
    const pairs=new Map();
    for(const flow of captured.flows.records||[]){
      if(!flow.device_mac||!flow.remote_ip||Date.now()-Date.parse(flow.last_seen)>=300000)continue;
      const source=flow.device_mac.toLowerCase(),identity=source+'|'+flow.remote_ip;
      const row=pairs.get(identity)||{source_mac:source,source_ip:flow.local_ip,destination:flow.remote_ip,bytes:0,mbps:0,
        upload_mbps:0,download_mbps:0,upload_bytes:0,first_seen:flow.first_seen,last_seen:flow.last_seen,interface:flow.interface,
        basis:'Bidirectional client-side IP flow accounting',bidirectional:true};
      row.bytes+=(flow.upload_bytes||0)+(flow.download_bytes||0);
      row.upload_bytes+=flow.upload_bytes||0;
      row.upload_mbps+=flow.upload_mbps||0;row.download_mbps+=flow.download_mbps||0;
      row.mbps=row.upload_mbps+row.download_mbps;
      if(flow.first_seen<row.first_seen)row.first_seen=flow.first_seen;
      if(flow.last_seen>row.last_seen)row.last_seen=flow.last_seen;
      pairs.set(identity,row);
    }
    for(const [identity,row]of pairs)uniqueFlows.set(identity,row);
  }
  const trafficSource=inventory.has(requested)?requested:data.viewer.id;
  const recent = [...uniqueFlows.values()].filter(row=>!allConnections||(row.source_mac===trafficSource&&(!row.bidirectional||row.upload_bytes>0)));
  const totals = new Map();
  for (const row of recent) totals.set(row.destination, (totals.get(row.destination) || 0) + row.bytes);
  const priority = new Set(recent.filter(row => row.source_mac === requested).map(row => row.destination));
  const destinations = [...totals.keys()].sort((a,b) =>
    Number(priority.has(b))-Number(priority.has(a)) || totals.get(b)-totals.get(a) || a.localeCompare(b)).slice(0,allConnections?totals.size:75);
  // Keep a selected retained destination selectable even below the top-volume cutoff.
  const selectedDestination = requested?.startsWith('external:') ? requested.slice(9) : null;
  if (selectedDestination && totals.has(selectedDestination) && !destinations.includes(selectedDestination)) {
    destinations.pop(); destinations.push(selectedDestination);
  }
  const shown = new Set(destinations);
  const relationships = recent.filter(row => shown.has(row.destination))
    .sort((a,b) => Number(b.source_mac === requested || b.destination === selectedDestination)-Number(a.source_mac === requested || a.destination === selectedDestination) || b.bytes-a.bytes).slice(0,allConnections?recent.length:200);
  for (const row of relationships) {
    if (!inventory.has(row.source_mac)) add({id:row.source_mac, mac:row.source_mac, ip:row.source_ip,
      title:'Observed LAN source', passive:true, ports:[]});
    if (!byIP(row.destination)) add({id:'external:'+row.destination, ip:row.destination, title:row.destination,
      external:true, context:true, ports:[]});
  }
  const requestedId = requested?.startsWith('external:') ? byIP(requested.slice(9))?.id : requested;
  const id = inventory.has(requestedId) ? requestedId : data.viewer.id;
  const observation = data.perspectives?.[id], measured = observation?.status === 'measured';
  const own = [...inventory.values()].find(node => node.self);
  const graph = new Map(), evidence = new Map();
  function connect(a, b, properties = {}) {
    if (a === b || !inventory.has(a) || !inventory.has(b)) return;
    const pair = [a, b].sort().join('|');
    const previous = graph.get(pair);
    const next = {a, b, capacity_mbps: null, actual_mbps: null, ...properties};
    const evidence = [...new Set([...(previous?.evidence_sources || []), properties.evidence].filter(Boolean))];
    // One visible edge; traffic relationships must not replace physical or route measurements.
    graph.set(pair, previous ? {...previous, evidence_sources:evidence,
      relationships:[...(previous.relationships || []), next]} : {...next, evidence_sources:evidence, relationships:[]});
  }
  function accessMetrics(client, view) {
    const rates = [client?.tx_link_mbps, client?.rx_link_mbps, view?.link?.capacity_mbps].filter(value => value > 0);
    const traffic = captured?.peers?.[client?.mac];
    return {capacity_mbps: fresh(router,45) && rates.length ? Math.max(...rates) : null,
      capacity_basis: client?.wireless ? 'Firmware-reported Wi-Fi PHY rate at '+router.observed+'; available bandwidth is not measured' : view?.link?.capacity_basis || 'Unknown LAN attachment rate',
      actual_mbps: traffic ? total(traffic) : captured ? 0 : null,
      traffic_basis: captured ? 'Traffic crossing this gateway only; peer-to-peer traffic inside ASUS is not visible' : 'No current per-peer capture',
      sampled: false, observed: captured?.updated};
  }
  function trace(source, record, includeFirstMetrics) {
    let previous = source;
    for (const hop of record?.route?.hops || []) {
      const first = hop.ttl === 1;
      const hopIP = hop.ip || (first ? record.gateway : null);
      const next = hopIP ? address(hopIP, 'Route hop') : add({id: `gap:${source}:${hop.ttl}`, title: `Hop ${hop.ttl} · no reply`, context: true, gap: true, ports: []});
      const local = source === own?.id;
      const link = local ? data.links?.[record.gateway_interface] : record.link;
      const traffic = local ? data.interface_traffic?.[record.gateway_interface] : record.traffic;
      connect(previous, next, {kind: hop.ip && inventory.get(previous)?.ip ? 'route' : 'route-gap',
        evidence: `UDP TTL ${hop.ttl} probe from ${record.hostname || source}${Number.isFinite(hop.rtt_ms) ? ' · '+hop.rtt_ms+' ms' : first && record.gateway ? ' · next hop known from default route; no TTL reply' : ' · no response'}`,
        capacity_mbps: first && includeFirstMetrics ? link?.capacity_mbps : null,
        capacity_basis: first && includeFirstMetrics ? 'Access-link rate ceiling; this IP hop may cross a bridge' : 'Unknown transit capacity',
        actual_mbps: first && includeFirstMetrics && (local ? fresh(traffic) : false) ? total(traffic) : null,
        traffic_basis: first && includeFirstMetrics ? 'Aggregate traffic on the observed interface; not per destination' : 'No transit-link traffic measurement',
        observed: traffic?.updated});
      previous = next;
    }
    if (!record?.route?.hops?.length && record?.gateway) connect(source, address(record.gateway, 'Upstream gateway'), {kind:'route', evidence:'Configured default gateway; TTL trace not yet available'});
  }
  if (!allConnections && layer === 'ip') {
    if (measured) trace(id, observation, true);
  } else if (!allConnections) {
    if (ap) {
      for (const client of router.clients || []) {
        if (!inventory.has(client.mac) || client.mac === ap) continue;
        const local = client.mac === own?.id;
        const view = data.perspectives?.[client.mac];
        const info = local ? data.links?.[data.interface] : view?.link;
        const sameBssid = info?.bssid && info.bssid === router.mac;
        const wireless = client.wireless || sameBssid;
        const traffic = data.interface_traffic?.[data.interface];
        connect(client.mac, ap, {kind: wireless ? 'wireless' : local ? 'ethernet' : 'lan-attachment',
          evidence: wireless ? 'ASUS wireless association table' : 'ASUS LAN membership; intermediate switches are unknown',
          ...(local ? {capacity_mbps: info?.capacity_mbps, capacity_basis: info?.capacity_basis,
            actual_mbps: fresh(traffic) ? total(traffic) : null, observed: traffic?.updated,
            traffic_basis: 'Aggregate measured Ethernet interface traffic'} : accessMetrics(client, view))});
      }
    } else if (measured && observation.devices?.length) {
      const segment = add({id:'segment:'+id, title:'Shared LAN · attachment unknown', context:true, ports:[]});
      connect(id, segment, {kind:'unresolved',evidence:'Neighbor table establishes a shared IP link, not a physical cable'});
      for (const device of observation.devices) connect(segment, key(device), {kind:'unresolved',evidence:'Neighbor observation; attachment path unknown'});
    }
    // The gateway's observed internet trace extends the L2 map without treating ASUS as an IP router.
    if (own && data.perspectives?.[own.id]?.status === 'measured') {
      const record = data.perspectives[own.id];
      trace(own.id, record, true);
    }
    if (!ap && measured && !graph.size) trace(id, observation, false);
  }
  for (const row of relationships) {
    connect(row.source_mac, byIP(row.destination).id, {kind:row.bidirectional?'observed-connection':'outbound',
      traffic_scope:byIP(row.destination).mac?'local':'external',
      upload_mbps:row.upload_mbps,download_mbps:row.download_mbps,
      evidence:row.bidirectional?'Observed bidirectional endpoint relationship; not a physical link':'Observed outbound IP traffic relationship; routed path, not a direct physical link',
      capacity_basis:'Not measured; a traffic relationship has no physical link capacity',
      actual_mbps:Number.isFinite(row.mbps) ? row.mbps : null,
      traffic_basis:row.bidirectional?captured.flows.scope:outbound.scope, observed:captured.updated, first_seen:row.first_seen,
      last_seen:row.last_seen, bytes:row.bytes, source_ip:row.source_ip,
      capture_interface:row.interface, attribution:row.basis});
  }
  const hardware=router?.hardware_flows;
  if(allConnections&&fresh(hardware,45))for(const row of hardware.relationships||[]){
    if(row.source_mac!==id)continue;
    for(const mac of [row.source_mac,row.destination_mac])if(!inventory.has(mac))add({id:mac,mac,title:'Observed LAN device',ports:[]});
    connect(row.source_mac,row.destination_mac,{kind:'outbound',
      traffic_scope:'local',evidence:'Router hardware-observed LAN MAC delivery',traffic_basis:hardware.basis,
      observed:hardware.observed,hardware_packets:row.hardware_packets,
      capacity_basis:'Hardware packet counters do not measure byte throughput',actual_mbps:null});
  }
  for (const device of observation?.devices || []) evidence.set(key(device), device);
  const allEdges = [...graph.values()], distances = new Map([[id,0]]), queue = [id];
  while (queue.length) {
    const current = queue.shift();
    for (const edge of allEdges) {
      const other = edge.a === current ? edge.b : edge.b === current ? edge.a : null;
      if (other && !distances.has(other)) { distances.set(other, distances.get(current)+1); queue.push(other); }
    }
  }
  const edges = allConnections?allEdges:allEdges.filter(edge => distances.has(edge.a) && distances.has(edge.b));
  const connected=new Set(edges.flatMap(edge=>[edge.a,edge.b]));
  const ipHops = new Map((observation?.route?.hops || []).filter(hop=>hop.ip).map(hop=>[hop.ip,hop.ttl]));
  if (observation?.gateway && !ipHops.has(observation.gateway)) ipHops.set(observation.gateway, 1);
  const nodes = [...inventory.values()].filter(device => allConnections ? device.id===id||connected.has(device.id) : !device.context||distances.has(device.id)||device.id===id).map(device => ({...device,
    title: device.mac === '7c:10:c9:b3:c2:38' ? 'ASUS access point' : device.title,
    perspective: device.id === id, you: device.id === data.viewer.id,
    visibility: device.id === id ? 'source' : distances.has(device.id)||allConnections&&connected.has(device.id) ? 'observed' : 'inventory',
    distance: distances.get(device.id)??(allConnections&&connected.has(device.id)?2:undefined), ip_hop: ipHops.get(device.ip),
    evidence: evidence.get(device.id), test:data.checks?.results?.[device.id], remote_info:data.remote_ips?.[device.ip],
    traffic_summary: (() => {
      const flows = recent.filter(row => row.source_mac === device.id || row.destination === device.ip);
      return {pairs:flows.length, destinations:new Set(flows.filter(row=>row.source_mac===device.id).map(row=>row.destination)).size,
        sources:new Set(flows.map(row=>row.source_mac)).size, bytes:flows.reduce((sum,row)=>sum+(row.bytes||0),0),
        basis:allConnections?'Retained observed endpoint IP bytes; logarithmic scale':undefined,
        mbps:outbound?.available||allConnections&&captured?.flows ? flows.reduce((sum,row)=>sum+(row.mbps||0),0) : null,
        first_seen:flows.map(row=>row.first_seen).filter(Boolean).sort()[0],
        last_seen:flows.map(row=>row.last_seen).filter(Boolean).sort().at(-1)};
    })()}));
  return {id, nodes, edges, observation, measured, layer, allConnections, outbound:allConnections&&captured?.flows?{...outbound,scope:captured.flows.scope}:outbound, source:nodes.find(node=>node.id===id)};
}
