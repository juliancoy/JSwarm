import {snapshotFetch} from './snapshot-fetch.js';
import {pollVisible} from './visible-poll.js';
import {EdgeLines} from './edge-lines.js';
const edgeLines=new EdgeLines();
import {selectHostTerminal} from './host-terminal.js';
import {networkView, label, edgeStyle, nodeVisual, trafficParticles} from './model.js';
import {loadSettings,saveSettings} from './settings.js';
import {groupNetwork} from './providers.js';
import {geographicNodes, geographicEdge, separateGeographicNodes, project} from './geography.js';
import {EdgeHistory,edgeID,edgeChart,hitEdge} from './edge-throughput.js';
import {createEdgeHover} from './edge-hover.js';

const $ = id => document.getElementById(id);
const canvas = $('graph'), ctx = canvas.getContext('2d');
const edgeHistory=new EdgeHistory(),edgeHover=createEdgeHover(document.querySelector('.scene'));
let edgeCharts=[],hoverPoint=null,hoveredEdge=null,lastEdgeHover=0,coordinatorContext=null;
function clearEdgeHover(){hoverPoint=null;hoveredEdge=null;edgeHover.hide();canvas.removeAttribute('aria-describedby');canvas.style.cursor='grab';}
let data, view, nodes = [], edges = [];
let perspective = new URLSearchParams(location.search).get('device');
let selectedId = perspective;
const embeddedLAN = new URLSearchParams(location.search).get('embed') === 'lan';
if (embeddedLAN) {
  document.documentElement.classList.add('embedded-lan');
  const controls=document.createElement('div');controls.className='embedded-map-controls';
  const count=document.createElement('span');count.id='lan-node-count';count.setAttribute('role','status');controls.append(count);
  controls.append($('lan-only').closest('label'),$('all-connections').closest('label'));
  for(const id of ['mine','pause','fit','zoom-in','zoom-out'])controls.append($(id));
  document.querySelector('.scene').append(controls);
}
const settings=loadSettings();
if(embeddedLAN)Object.assign(settings,{layer:'links',geographic:false,groupProviders:false,labels:true});
let layer = settings.layer;
let allConnections=settings.allConnections;
let lanOnly=settings.lanOnly;
$('lan-only').checked=lanOnly;
let geographic = settings.geographic&&!allConnections, land, geoRequested=false;
let groupProviders = settings.groupProviders;
let resolveNames=settings.resolveNames;
fetch('/land.geojson').then(response=>response.json()).then(value=>{land=value;}).catch(()=>{});
const motionPreference = matchMedia('(prefers-reduced-motion: reduce)');
let paused = settings.paused || motionPreference.matches;
motionPreference.addEventListener('change', () => { paused = settings.paused || motionPreference.matches; pauseUI(); });
let labels = settings.labels, filter = '', width = 1, height = 1, zoom = 1;
let pan = {x: 0, y: 0}, gesture, last = 0, frames = 0, frameStamp = 0;
let detailSignature;
let particleTime = 0;
let geographicPositions=new Map(),geographicSignature='';
const positionCache = new Map();
const resolvingIPs = new Set();
$('all-connections').checked=allConnections;
$('layer').disabled=allConnections;
$('group-providers').disabled=allConnections;
$('geographic').disabled=allConnections;
$('geographic').checked=geographic;
$('group-providers').checked=groupProviders;
$('labels').checked=labels;
$('resolve-names').checked=resolveNames;
$('layer').value=layer;
$('repulsion').value=settings.repulsion;
$('force-value').value=settings.repulsion;
function persistSettings(){
  const saved=saveSettings({layer,lanOnly,allConnections,geographic,groupProviders,labels,paused:settings.paused,resolveNames,repulsion:Number($('repulsion').value)});
  $('settings-status').textContent=saved?'Settings saved in this browser.':'Browser storage is unavailable; settings last for this session.';
}
function requestGeography(){
  if(!geographic||geoRequested||!data)return;
  geoRequested=true;
  fetch('/api/geography',{method:'POST'}).then(response=>{if(!response.ok)throw Error('HTTP '+response.status);}).catch(error=>{geoRequested=false;$('error').textContent='Location lookup unavailable: '+error.message;});
}


function element(tag, text, className) {
  const node = document.createElement(tag);
  node.textContent = text;
  if (className) node.className = className;
  return node;
}
function field(container, title, value) {
  if (value === undefined || value === null || value === '') return;
  const row = element('div', '', 'field');
  row.append(element('label', title), element('span', String(value)));
  container.append(row);
}
function matches(node) {
  return [label(node), node.ip, node.mac, node.ipv6, node.remote_info?.organization, node.remote_info?.network_domain, node.remote_info?.asn, ...(node.members || []).flatMap(member=>[member.ip,label(member)])].join(' ').toLowerCase().includes(filter);
}
function color(node) {
  if (node.self) return '#ad9fe7';
  if (node.id === data?.router?.mac) return '#72b9ef';
  if (node.external) return '#e8ac73';
  if (node.context) return '#b9a8cf';
  return '#b7e4cf';
}

function choose(id) {
  const node=nodes.find(node=>node.id===id);
  perspective = node?.provider_group ? node.members[0].id : id;
  selectedId = id;
  const query=new URLSearchParams();
  if(embeddedLAN)query.set('embed','lan');
  if(id!==data.viewer.id)query.set('device',perspective);
  history.replaceState(null,'',location.pathname+(query.size?'?'+query:''));
  sync();
}
function sync() {
  let rawView = networkView(data, perspective, layer, allConnections);
  if(lanOnly){
    const visible=rawView.nodes.filter(node=>!node.external);
    const ids=new Set(visible.map(node=>node.id));
    rawView={...rawView,nodes:visible,edges:rawView.edges.filter(edge=>ids.has(edge.a)&&ids.has(edge.b))};
  }
  perspective = rawView.id;
  const remote=rawView.nodes.filter(node=>!node.mac&&node.ip);
  const ipv6=remote.filter(node=>node.ip.includes(':'));
  const named=ipv6.filter(node=>node.remote_info?.ptr).length;
  const absent=ipv6.filter(node=>['NXDOMAIN','NOERROR'].includes(node.remote_info?.dns_status)&&!node.remote_info?.ptr).length;
  const pending=ipv6.length-named-absent;
  $('dns-summary').textContent=`IPv6: ${named} named · ${absent} without PTR · ${pending} pending / failed. ${data.name_resolution?.pending||0} lookups queued. ${resolveNames?'Automatic lookup on.':'Automatic lookup paused in this browser.'}`;
  requestGeography();

  view = groupProviders&&!allConnections ? groupNetwork(rawView) : rawView;
  if(embeddedLAN){
    view={...view,nodes:view.nodes.filter(node=>!lanOnly&&allConnections||!node.external)};
    const ids=new Set(view.nodes.map(node=>node.id));
    view.edges=view.edges.filter(edge=>ids.has(edge.a)&&ids.has(edge.b));
    $('lan-node-count').textContent=view.nodes.length+(!lanOnly&&allConnections?' traffic nodes · direct endpoints':' LAN nodes');
  }
  if (!view.nodes.some(node => node.id === selectedId)) selectedId = view.id;

  const old = new Map(nodes.map(node => [node.id, node]));
  const stamp = performance.now();
  for (const node of nodes) {
    positionCache.delete(node.id);
    positionCache.set(node.id, {x:node.x, y:node.y, vx:node.vx, vy:node.vy, seen:stamp});
  }
  for (const [id, saved] of positionCache) if (stamp-saved.seen > 300000) positionCache.delete(id);
  while (positionCache.size > 2048) positionCache.delete(positionCache.keys().next().value);
  nodes = view.nodes.map((node, i) => {
    const prior = old.get(node.id) || positionCache.get(node.id);
    const radius = node.visibility === 'inventory' ? 540 : Math.min(4, node.distance || 1) * 190;
    const angle = i * 2.4;
    const targetX=(layer==='ip'&&!allConnections) ? node.visibility==='inventory' ? (i%6)*145 : (node.distance||0)*145 : Math.cos(angle)*radius;
    const targetY=(layer==='ip'&&!allConnections) ? node.visibility==='inventory' ? 250+Math.floor(i/6)*90 : 0 : Math.sin(angle)*radius;
    return {...node, targetX,targetY, x: prior ? prior.x : node.perspective ? 0 : targetX,
      y: prior ? prior.y : node.perspective ? 0 : targetY,
      vx: prior?.vx || 0, vy: prior?.vy || 0,
      visual: nodeVisual(node, data.perspectives?.[node.id]),
      radius: nodeVisual(node, data.perspectives?.[node.id]).radius};
  });
  if (geographic) nodes = geographicNodes(nodes, data.geography);
  document.body.dataset.geographic = String(geographic);
  $('geo-caption').hidden = !geographic;
  if (geographic) {
    const located = nodes.filter(node=>node.geographic).length;
    $('geo-caption').textContent = `${located}/${nodes.length} located · IP estimates, not GPS · LAN shares estimated gateway site · collision spacing separates devices; tethers mark locations · ${data.geography?.running ? 'Looking up locations… · ' : ''}${nodes.length-located} unlocated: available in the device list · Land: Natural Earth (public domain)`;
  }
  edges = view.edges;
  edgeHistory.update(edges,nodes,data);
  edgeLines.update(edges);
  if (gesture?.node) gesture.node = nodes.find(node => node.id === gesture.node.id);
  document.body.dataset.ready = 'true';
  document.body.dataset.viewer = data.viewer.id;
  document.body.dataset.perspective = view.id;
  document.body.dataset.measured = String(view.measured);
  document.body.dataset.selected = selectedId;
  document.body.dataset.layer = layer;
  document.body.dataset.directTraffic=String(allConnections);
  $('viewer').textContent = 'Your connection: ' + data.viewer.ip + (data.viewer.recognized ? '' : ' · not yet identified');
  $('perspective-caption').textContent = view.id === data.viewer.id ? 'Your perspective' : 'Viewing from another device';
  $('perspective-title').textContent = label(view.source) + (view.id === data.viewer.id ? ' · You' : '');
  const observation = view.observation;
  $('perspective-status').textContent = allConnections ? `${edges.length} observed destinations sent to · last 5 minutes · capture coverage is partial` : view.source.external ? 'Passive destination perspective · dotted edges are observed routed traffic, not physical links.' : view.measured
    ? `${observation.interface} · ${observation.subnet} · ${edges.length} ${(layer === 'ip'&&!allConnections) ? 'route segments' : 'mapped links'}`
    : edges.length ? 'Infrastructure associations show this device’s attachment; no device login is available.' : 'No observations from this device. Dim particles are shared inventory.';
  $('updated').textContent = data.scanning || data.mapping ? 'Refreshing observations…' : 'Updated ' + new Date(data.updated || Date.now()).toLocaleTimeString();
  $('refresh').disabled = !!(data.scanning || data.mapping);
  $('web-checks').disabled = !!data.checks?.running;
  $('web-checks').textContent = data.checks?.running ? 'Testing…' : 'Run web tests';
  const counts = {};
  for (const result of Object.values(data.checks?.results || {})) counts[result.status] = (counts[result.status] || 0) + 1;
  $('test-summary').textContent = Object.entries(counts).map(([status, count]) => `${count} ${status}`).join(' · ') || 'Ready to test available devices.';
  $('error').textContent = data.error || '';
  renderList();
  renderHistory();
  renderDetails();
  const selected = nodes.find(node=>node.id===selectedId);
  if (resolveNames && selected?.ip && !selected.mac && !selected.remote_info && !resolvingIPs.has(selected.ip)) {
    resolvingIPs.add(selected.ip);
    fetch('/api/resolve?ip='+encodeURIComponent(selected.ip),{method:'POST'}).catch(()=>{resolvingIPs.delete(selected.ip);});
  }
  pauseUI();
  if (!old.size) {
    requestAnimationFrame(fit);
  }
}
function renderList() {
  const container = $('devices');
  const focused = container.contains(document.activeElement) ? document.activeElement.dataset.device : null;
  container.replaceChildren();
  for (const node of nodes.filter(matches)) {
    const button = element('button', label(node)+(node.provider_group ? ' · '+node.members.length+' endpoints' : ''), 'device' + (node.id === selectedId ? ' active' : ''));
    button.dataset.device = node.id;
    if(node.hostname)button.dataset.terminalHost=node.hostname;
    button.setAttribute('aria-pressed', String(node.perspective));
    button.append(element('small', node.ip || 'Network context'));
    if(geographic)button.append(element('small',node.geographic?'Mapped · '+[node.geographic.city,node.geographic.country].filter(Boolean).join(', '):'Location unknown · omitted from map'));
    button.append(element('small', node.visual.health+' · '+node.visual.reason));
    if (node.you) button.append(element('small', 'Your device', 'tag'));
    const status = node.perspective ? 'Current perspective' : node.visibility === 'observed' ? 'Observed from this device' : 'Known elsewhere · not observed here';
    button.append(element('small', status));
    if (node.test?.transfer) button.append(element('small', `${node.test.transfer.mbps.toFixed(1)} Mbps · last download test`));
    button.onclick = () => choose(node.id);
    container.append(button);
  }
  if (!container.children.length) container.append(element('p', 'No matching devices.', 'muted'));
  if (focused) [...container.children].find(button => button.dataset.device === focused)?.focus({preventScroll: true});
}
function renderHistory() {
  const container = $('device-history');
  const records = data.device_history || [];
  $('history-count').textContent = '('+records.length+')';
  const open = new Set([...container.querySelectorAll('details[open]')].map(row=>row.dataset.mac));
  container.replaceChildren();
  for (const record of records.filter(record => [record.mac,...record.addresses,...record.names.map(n=>n.name)].join(' ').toLowerCase().includes(filter))) {
    const section = element('details', '', 'history-device');
    section.dataset.mac = record.mac;
    section.open = open.has(record.mac);
    const names = [...new Set(record.names.map(item=>item.name))];
    section.append(element('summary', names.join(' / ') || record.mac));
    field(section, 'MAC address', record.mac);
    field(section, 'Known addresses', record.addresses.join(' · '));
    field(section, 'First recorded', new Date(record.first_observed).toLocaleString());
    field(section, 'Last observation', new Date(record.last_observed).toLocaleString());
    field(section, 'Last confirmed reachable / associated', record.last_confirmed ? new Date(record.last_confirmed).toLocaleString() : 'Never confirmed; remembered or neighbor-table record only');
    field(section, 'Previously responding ports', record.ports.join(', ') || 'None recorded');
    field(section, 'Evidence sources', record.sources.join(' · '));
    for (const name of record.names) field(section, 'Name history · '+name.source, name.name+' · '+new Date(name.first_seen).toLocaleString()+' → '+new Date(name.last_seen).toLocaleString());
    const mapped = nodes.find(node=>node.id===record.mac);
    if (mapped) {
      const button = element('button', 'View on map');
      button.onclick = ()=>choose(mapped.id);
      section.append(button);
    }
    container.append(section);
  }
  if (!container.children.length) container.append(element('p', 'No matching history records.', 'muted'));
}

const formatBytes = value => value >= 1048576 ? (value/1048576).toFixed(2)+' MiB' : value >= 1024 ? (value/1024).toFixed(2)+' KiB' : value+' B';
const rate = value => Number.isFinite(value) ? (value >= 1000 ? (value/1000).toFixed(2)+' Gbps' : value.toFixed(2)+' Mbps') : 'Unknown';
function renderDetails() {
  const node = nodes.find(n => n.id === selectedId) || view.source;
  const observation = data.perspectives?.[node.id], container = $('details');
  const incident = edges.filter(edge => edge.a === node.id || edge.b === node.id);
  const signature = JSON.stringify([node, observation, incident, node.id === data.router?.mac ? data.router.telemetry : null, view.outbound?.available]);
  if (signature === detailSignature) return;
  detailSignature = signature;
  const newSelection=container.dataset.node!==node.id;
  container.dataset.node=node.id;
  container.replaceChildren(element('h2', 'Selected node'), element('h3', label(node)));
  if(newSelection)container.scrollTop=0;
  field(container, 'Address', node.provider_group ? node.members.length+' grouped endpoints' : node.ip || 'No reply to TTL probe');
  if(node.provider_group){
    field(container,'Grouping evidence','Known provider ASN or explicit domain family; network operator is not website ownership.');
    field(container,'Geographic scope','Provider aggregate can span multiple locations.');
    for(const member of node.members)field(container,member.ip,[label(member),member.remote_info?.asn?'AS'+member.remote_info.asn:null].filter(Boolean).join(' · '));
  }
  if (node.remote_info) {
    const remote = node.remote_info;
    field(container, 'Remote identity lookup', remote.status === 'resolving' ? 'Resolving…' : 'Cached public-IP lookup');
    field(container, 'Reverse hostname', remote.ptr_names?.length ? remote.ptr_names.join(' · ') : remote.ptr || remote.ptr_status);
    field(container, 'DNS response', remote.dns_status);
    field(container, 'DNS resolver', remote.dns_resolver);
    field(container, 'DNS query', remote.dns_command);
    field(container, 'DNS TTL / retry interval', Number.isFinite(remote.dns_ttl) ? remote.dns_ttl+' seconds' : null);
    field(container, 'Network operator', remote.organization);
    field(container, 'Autonomous system', remote.asn ? 'AS'+remote.asn : null);
    field(container, 'ISP / network domain', [remote.isp,remote.network_domain].filter(Boolean).join(' · '));
    field(container, 'Approximate IP location', [remote.city,remote.region,remote.country].filter(Boolean).join(' · '));
    field(container, 'Lookup sources', remote.sources?.join(' · '));
    field(container, 'Lookup time', remote.observed);
    field(container, 'Unavailable network data', remote.network_error);
    field(container, 'Website attribution', 'A hostname or network operator does not identify the visited website. Shared CDN / proxy IPs can serve many domains; anycast location may differ from the server reached.');
  }
  if (node.external || incident.some(edge => edge.kind === 'outbound')) {
    field(container, 'Passive outbound observation', view.outbound?.available ? view.outbound.scope : 'Unknown: no current capture');
    field(container, 'Retention / display', '5-minute idle expiry; 1,024 source/destination pairs retained; at most 75 destinations and 200 traffic relationships shown, prioritizing selected local source');
  }
  if (node.external) field(container, 'Node type', 'Observed external destination; no remote login or active device probe');
  if (!node.external) field(container, 'Outbound traffic visibility', view.outbound?.available ?
    'Only traffic captured at this gateway is known. No displayed destination does not mean no traffic.' : 'Unknown: capture unavailable or stale');
  field(container, 'IPv6', node.ipv6);
  field(container, 'MAC address', node.mac);
  field(container, 'Known addresses', node.addresses?.join(' · '));
  if (geographic) {
    field(container, 'Geographic location', node.geographic ? [node.geographic.city,node.geographic.region,node.geographic.country].filter(Boolean).join(' · ') : 'Unknown; available in the device list, not plotted on the map');
    field(container, 'Coordinates', node.geographic ? node.geographic.latitude+', '+node.geographic.longitude : null);
    field(container, 'Location basis', node.geographic ? (node.geographic.shared_site ? 'Shared gateway site estimate; individual device position unknown' : 'Approximate IP geolocation; anycast and VPN locations may differ')+' · '+node.geographic.source+' · '+node.geographic.observed : null);
  }
  field(container, 'Observed names', [...new Set([node.hostname,node.router_name].filter(Boolean))].join(' · '));
  field(container, 'Mapped connections', incident.length);
  field(container, 'Health ring', node.visual.health+' · '+node.visual.reason);
  field(container, 'Particle size', node.visual.size_basis);
  const summary = node.traffic_summary;
  if (summary?.pairs) {
    field(container, 'Retained traffic relationships', summary.pairs+' unique source / destination pairs');
    field(container, 'Distinct outbound destinations', summary.destinations);
    field(container, 'Observed source devices', summary.sources);
    field(container, 'Retained outbound volume', formatBytes(summary.bytes));
    field(container, 'Current outbound rate', rate(summary.mbps));
    field(container, 'Traffic first / last seen', summary.first_seen+' / '+summary.last_seen);
    field(container, 'Summary scope', 'Gateway capture, retained pairs only; outbound IP bytes, not total device traffic');
  }
  field(container, 'Position in network', node.external ? 'Observed internet destination · passive perspective' : node.ip_hop ? 'IP hop '+node.ip_hop+' from this perspective' : node.id === data.router?.mac ? 'Layer 2 access point · not an IP routing hop' : node.perspective ? 'Current viewing device' : node.visibility === 'inventory' ? 'Known elsewhere; attachment not observed' : 'Mapped from infrastructure evidence');
  field(container, 'Device-reported observation', observation?.status === 'measured' ? 'Available' : 'Not available');
  if (node.association) {
    field(container, 'Association observed', new Date(node.association.observed).toLocaleTimeString());
    field(container, 'ASUS attachment', node.association.wireless ? 'Wi-Fi · band '+node.association.band : 'LAN bridge');
    field(container, 'Negotiated Wi-Fi rate to client', rate(node.association.tx_link_mbps));
    field(container, 'Negotiated Wi-Fi rate from client', rate(node.association.rx_link_mbps));
    field(container, 'Signal', node.association.signal_dbm ? node.association.signal_dbm+' dBm' : 'Unknown');
    field(container, 'Association age', Date.now()-Date.parse(node.association.observed) < 45000 ? 'Current (firmware refresh every 15 seconds)' : 'Stale: historical association and PHY rates');
    field(container, 'Firmware connection duration', node.association.connected_duration);
    field(container, 'Firmware per-client byte counters', 'Unavailable on current AP firmware; gateway observations shown separately');
  }
  if (node.id === data.router?.mac && data.router.telemetry) {
    const telemetry = data.router.telemetry, firmware = telemetry.firmware || {};
    field(container, 'Router firmware', [firmware.productid, firmware.firmver, firmware.buildno && firmware.buildno+'_'+firmware.extendno].filter(Boolean).join(' · '));
    field(container, 'Router mode', firmware.sw_mode === '3' ? 'Access point / bridge (reported mode 3)' : firmware.sw_mode || 'Unknown');
    field(container, 'Router analytics observed', telemetry.observed);
    field(container, 'Router analytics freshness', Date.now()-Date.parse(telemetry.observed) < 45000 ? 'Current · 15-second refresh' : 'Stale · current values unknown');
    field(container, 'Router telemetry sources', telemetry.sources?.join(' · '));
    field(container, 'Router WAN', telemetry.wan);
    field(container, 'Router destination records', telemetry.destination_records);
    field(container, 'Router per-client counters', telemetry.per_client_traffic);
    const health = telemetry.health;
    if (health) {
      field(container, 'CPU load averages (1 / 5 / 15 minutes)', health.load_average_1_5_15?.join(' / '));
      field(container, 'Router memory used / total', health.memory_used_mb+' / '+health.memory_total_mb+' MB (firmware units)');
      field(container, 'Router memory available / free', health.memory_available_mb+' / '+health.memory_free_mb+' MB');
      field(container, 'Router conntrack entries / active', health.conntrack_entries+' / '+health.active_connections+' (router only, not total LAN flows)');
      field(container, 'Health measurement basis', health.basis);
    }
    for (const [name,value] of Object.entries(telemetry.temperatures || {})) field(container, name.replaceAll('_',' '), Number.isFinite(value) ? value+' °C' : 'Unknown');
    for (const [name,counters] of Object.entries(telemetry.interfaces || {})) {
      field(container, name+' router RX / TX', rate(counters.rx_mbps)+' / '+rate(counters.tx_mbps));
      field(container, name+' cumulative RX / TX bytes', counters.rx_bytes+' / '+counters.tx_bytes);
    }
    field(container, 'Router counter scope', telemetry.interface_basis);
    field(container, 'Router counter sample seconds', telemetry.sample_seconds?.toFixed(2));
    field(container, 'Unavailable telemetry', Object.entries(telemetry.errors || {}).map(([key,value]) => key+': '+value).join(' · '));
  }
  if (observation?.status === 'measured') {
    field(container, 'Interface', observation.interface);
    field(container, 'Default gateway', observation.gateway);
    field(container, 'Observed at', new Date(observation.observed).toLocaleString());
    if (observation.internet_probe) field(container, 'Internet ping', observation.internet_probe.success ? 'Replied from '+observation.internet_probe.target : observation.internet_probe.output);
    if (observation.route) {
      field(container, 'IP route to '+observation.route.target, observation.route.hops.map(h => h.ttl+': '+(h.ip || 'no reply')).join(' → '));
      field(container, 'Route completion', observation.route.complete ? 'Destination replied' : 'Partial; missing replies remain unknown');
    }
  } else if (observation?.error) field(container, 'Access status', observation.error);
  if (observation?.traffic) {
    field(container, 'Interface RX / TX', rate(observation.traffic.rx_mbps)+' / '+rate(observation.traffic.tx_mbps));
    field(container, 'Traffic observed', observation.traffic.updated || observation.observed);
  }
  field(container, 'Responding services', node.ports?.length ? node.ports.join(', ') : 'None observed');
  for (const edge of incident) {
    const other = nodes.find(n => n.id === (edge.a === node.id ? edge.b : edge.a));
    const section = element('div', '', 'edge-detail');
    const heading = element('button', (['outbound','observed-connection','provider-relationship'].includes(edge.kind) ? 'Observed traffic with ' : 'Link to ')+label(other), 'connection-title');
    heading.onclick = () => choose(other.id);
    section.append(heading);
    const style = edgeStyle(edge), swatch = element('div', '', 'connection-meter');
    swatch.style.setProperty('--link-width', style.outer+'px');
    swatch.style.setProperty('--traffic-width', style.inner+'px');
    swatch.style.setProperty('--traffic-color', style.color);
    swatch.dataset.known = String(edge.capacity_mbps > 0);
    swatch.append(element('span', '', 'connection-fill'));
    swatch.setAttribute('role', 'img');
    swatch.setAttribute('aria-label', 'Capacity '+rate(edge.capacity_mbps)+'; observed '+rate(edge.actual_mbps));
    section.append(swatch);
    field(section, 'Connection type', edge.kind);
    for (const relationship of edge.relationships || []) {
      field(section, 'Additional observed relationship', relationship.kind+' · '+rate(relationship.actual_mbps)+' · '+formatBytes(relationship.bytes || 0));
    }
    if (edge.kind === 'observed-connection') {
      field(section,'Upload / download',rate(edge.upload_mbps)+' / '+rate(edge.download_mbps));
      field(section,'Retained bidirectional IP bytes',formatBytes(edge.bytes));
      field(section,'Capture interface / attribution',edge.capture_interface+' · '+edge.attribution);
    }
    if (edge.kind === 'outbound') {
      field(section, 'Direction', edge.source_ip+' → '+nodes.find(n => n.id === edge.b)?.ip+' (outbound only)');
      field(section, 'Retained outbound IP bytes', formatBytes(edge.bytes));
      field(section, 'First / last seen', edge.first_seen+' / '+edge.last_seen);
      field(section, 'Capture interface / attribution', edge.capture_interface+' · '+edge.attribution);
    }
    field(section, 'Capacity ceiling', rate(edge.capacity_mbps));
    field(section, 'Capacity evidence', edge.capacity_basis || 'Unknown');
    field(section, 'Observed throughput', rate(edge.actual_mbps));
    field(section, 'Traffic scope', edge.traffic_basis || 'No measurement');
    if (edge.observed) field(section, 'Sample time', new Date(edge.observed).toLocaleTimeString());
    field(section, 'Topology evidence', edge.evidence_sources?.join(' · ') || edge.evidence);
    container.append(section);
  }
  if (node.test) {
    field(container, 'Web tests', node.test.status);
    field(container, 'Download benchmark', node.test.transfer ? rate(node.test.transfer.mbps)+' · '+node.test.transfer.path : 'Not measured');
    field(container, 'Test detail', node.test.error);
  }
  if (!node.external && node.ports?.includes(22)) {
    const address = node.ssh_ip || node.ip;
    field(container, 'SSH command', `ssh julian@${address}${address.includes(':') ? '%'+data.interface : ''}`);
  }
  if(newSelection)requestAnimationFrame(()=>{container.scrollTop=0;});
}

function resize() {
  const rectangle = canvas.getBoundingClientRect();
  width = rectangle.width; height = rectangle.height;
  const scale = Math.min(devicePixelRatio || 1, 2);
  canvas.width = width * scale; canvas.height = height * scale;
  ctx.setTransform(scale, 0, 0, scale, 0, 0);
  if(geographic&&nodes.length)requestAnimationFrame(fit);
}
new ResizeObserver(resize).observe(canvas);
function prepareGeographicPositions(){
  if(!geographic)return;
  const signature=JSON.stringify([zoom,nodes.map(n=>[n.id,n.x,n.y,n.radius,n.perspective,n.geographic?.offset])]);
  if(signature===geographicSignature)return;
  geographicSignature=signature;
  geographicPositions=separateGeographicNodes(nodes,zoom,node=>nodeScreenRadius(node)+14);
}
function screen(node) {
  if(geographic&&node.geographic){const p=geographicPositions.get(node.id)||{x:node.x*zoom,y:node.y*zoom};return {x:width/2+pan.x+p.x,y:height/2+pan.y+p.y};}
  return {x:width/2+pan.x+node.x*zoom,y:height/2+pan.y+node.y*zoom};
}
function world(point) { return {x: (point.x - width / 2 - pan.x) / zoom, y: (point.y - height / 2 - pan.y) / zoom}; }
function position(event) { const r = canvas.getBoundingClientRect(); return {x: event.clientX - r.left, y: event.clientY - r.top}; }
function nodeScreenRadius(node) {
  return geographic ? Math.max(4, Math.min(9, node.radius * .3)) : node.radius * zoom;
}
function hit(point) {
  prepareGeographicPositions();
  let nearest, closest = Infinity;
  for (const node of nodes) {
    if(geographic&&!node.geographic)continue;
    const p = screen(node), distance = Math.hypot(p.x-point.x,p.y-point.y);
    if (distance < Math.max(geographic ? 13 : 20, nodeScreenRadius(node)+6) && distance < closest) {
      nearest = node; closest = distance;
    }
  }
  return nearest;
}
canvas.onpointerdown = event => {
  const point = position(event);
  const edge=hitEdge(point,edgeCharts);clearEdgeHover();
  gesture = {point, start: point, node: hit(point), edge, moved: false};
  canvas.setPointerCapture(event.pointerId);
};
canvas.onpointermove = event => {
  if (!gesture) {
    const point=position(event),node=hit(point);
    if(node)clearEdgeHover();
    else{hoverPoint=point;hoveredEdge=hitEdge(point,edgeCharts);if(!hoveredEdge)edgeHover.hide();canvas.style.cursor=hoveredEdge?'help':'grab';}
    return;
  }
  clearEdgeHover();
  const point = position(event);
  gesture.moved ||= Math.hypot(point.x - gesture.start.x, point.y - gesture.start.y) > 6;
  if (gesture.moved) {
    if (gesture.node && !gesture.node.perspective && !geographic) {
      const p = world(point); Object.assign(gesture.node, p, {vx: 0, vy: 0});
    } else if (!gesture.node || geographic) {
      pan.x += point.x - gesture.point.x; pan.y += point.y - gesture.point.y;
    }
  }
  gesture.point = point;
};
canvas.onpointerup = () => { const selected = gesture?.node, moved = gesture?.moved,edge=gesture?.edge,point=gesture?.point; gesture = null; if (selected && !moved) {choose(selected.id);if(selected.hostname)selectHostTerminal(selected.hostname);}else if(edge&&!moved){hoverPoint=point;hoveredEdge=edge;edgeHover.show(edge,point,nodes,data,coordinatorContext);canvas.setAttribute('aria-describedby','edge-hover');} };
canvas.addEventListener('pointerleave',()=>{clearEdgeHover();canvas.style.cursor='grab';});
canvas.tabIndex=0;canvas.addEventListener('keydown',event=>{if(event.key==='Escape')clearEdgeHover();});
canvas.onpointercancel = () => { gesture = null;clearEdgeHover(); };
canvas.addEventListener('wheel', event => {
  event.preventDefault();clearEdgeHover();
  const p = position(event), before = world(p);
  zoom = Math.max(geographic?.08:.25, Math.min(64, zoom * Math.exp(-event.deltaY * .001)));
  pan.x = p.x - width / 2 - before.x * zoom; pan.y = p.y - height / 2 - before.y * zoom;
}, {passive: false});
function fit() {
  clearEdgeHover();
  if (!nodes.length || width < 2) return;
  if(geographic){
    zoom=Math.max(.08,Math.min(1.3,(width-100)/1440,(height-160)/720));
    pan={x:0,y:15};return;
  }
  const xs=nodes.map(n=>n.x),ys=nodes.map(n=>n.y);
  const minX=Math.min(...xs)-100,maxX=Math.max(...xs)+100,minY=Math.min(...ys)-100,maxY=Math.max(...ys)+100;
  zoom=Math.max(.25,Math.min(1.3,(width-80)/(maxX-minX),(height-260)/(maxY-minY)));
  pan={x:-(minX+maxX)/2*zoom,y:-(minY+maxY)/2*zoom};
}
function physics(dt) {
  if (geographic) return;
  const force = Number($('repulsion').value) * 130;
  for (let i = 0; i < nodes.length; i++) for (let j = i + 1; j < nodes.length; j++) {
    const a = nodes[i], b = nodes[j], dx = a.x - b.x, dy = a.y - b.y;
    const distance = Math.max(1, Math.hypot(dx, dy)), push = Math.min(3, force / (distance * distance)) * dt;
    a.vx += dx / distance * push; a.vy += dy / distance * push;
    b.vx -= dx / distance * push; b.vy -= dy / distance * push;
  }
  for (const edge of edges) {
    const a = nodes.find(node => node.id === edge.a), b = nodes.find(node => node.id === edge.b);
    const dx = b.x - a.x, dy = b.y - a.y, distance = Math.max(1, Math.hypot(dx, dy));
    const pull = (distance - 150) * .002 * dt;
    a.vx += dx / distance * pull; a.vy += dy / distance * pull;
    b.vx -= dx / distance * pull; b.vy -= dy / distance * pull;
  }
  for (const node of nodes) {
    if (node.perspective) {
      const step = 1 - Math.exp(-dt / 28);
      node.x += Math.max(-2, Math.min(2, -node.x * step)) * dt;
      node.y += Math.max(-2, Math.min(2, -node.y * step)) * dt;
      node.vx = node.vy = 0; continue;
    }
    if (gesture?.node === node) continue;
    const distance = Math.max(1, Math.hypot(node.x, node.y));
    if((layer==='ip'&&!allConnections)){node.vx=(node.vx+(node.targetX-node.x)*.02*dt)*.9;node.vy=(node.vy+(node.targetY-node.y)*.02*dt)*.9;node.x+=Math.max(-3,Math.min(3,node.vx))*dt;node.y+=Math.max(-3,Math.min(3,node.vy))*dt;continue;}
    const target = node.visibility === 'inventory' ? 560 : Math.min(4, node.distance || 1) * 190;
    node.vx = (node.vx - node.x / distance * (distance - target) * .0006 * dt) * Math.pow(.93, dt);
    node.vy = (node.vy - node.y / distance * (distance - target) * .0006 * dt) * Math.pow(.93, dt);
    node.x += Math.max(-3, Math.min(3, node.vx)) * dt;
    node.y += Math.max(-3, Math.min(3, node.vy)) * dt;
  }
}

function drawGeography() {
  ctx.save();ctx.lineWidth=1;ctx.strokeStyle='#284651';ctx.fillStyle='#142d32';ctx.globalAlpha=.85;
  const path = points => {points.forEach(([longitude,latitude],i)=>{const p=screen(project(longitude,latitude));if(i)ctx.lineTo(p.x,p.y);else ctx.moveTo(p.x,p.y);});};
  for (const feature of land?.features || []) {
    const polygons=feature.geometry.type==='Polygon' ? [feature.geometry.coordinates] : feature.geometry.coordinates;
    for (const polygon of polygons) {ctx.beginPath();for (const ring of polygon) {path(ring);ctx.closePath();}ctx.fill('evenodd');ctx.stroke();}
  }
  ctx.strokeStyle='#355360';ctx.globalAlpha=.45;
  for(let lon=-180;lon<=180;lon+=30){ctx.beginPath();path([[lon,-90],[lon,90]]);ctx.stroke();}
  for(let lat=-90;lat<=90;lat+=30){ctx.beginPath();path([[-180,lat],[180,lat]]);ctx.stroke();}
  ctx.fillStyle='#a8bac6';ctx.font='12px system-ui';ctx.globalAlpha=1;ctx.textAlign='left';

  ctx.globalAlpha=.4;
  for(const node of nodes.filter(node=>node.geographic)) {const a=screen(node.geographic.anchor),b=screen(node);ctx.beginPath();ctx.moveTo(a.x,a.y);ctx.lineTo(b.x,b.y);ctx.stroke();}
  ctx.restore();
}
function frame(time) {
  prepareGeographicPositions();
  const dt = Math.min(2, (time - last) / 16.67 || 1); last = time;
  if (!paused && !motionPreference.matches && !document.hidden) { physics(dt); particleTime += dt * 16.67; }
  ctx.clearRect(0, 0, width, height);
  ctx.fillStyle = '#334853'; ctx.globalAlpha = .3;
  for (let x = (width / 2 + pan.x) % 32; x < width; x += 32) for (let y = (height / 2 + pan.y) % 32; y < height; y += 32) ctx.fillRect(x, y, 1, 1);
  ctx.globalAlpha = 1;
  if (geographic) drawGeography();
  const geographicNodesByID=geographic?new Map(nodes.map(node=>[node.id,node])):null;
  const nextEdgeCharts=[];
  for (const edge of edges) {
    if(geographic&&!geographicEdge(edge,geographicNodesByID))continue;
    const a = nodes.find(node => node.id === edge.a), b = nodes.find(node => node.id === edge.b);
    const p = screen(a), q = screen(b), style = edgeStyle(edge);
    const scope=edge.traffic_scope;
    const trafficColor=scope==='local'?'#44d9e6':scope==='external'?'#ffac59':null;
    if(trafficColor)style.color=trafficColor;
    const adjacent = edge.a === selectedId || edge.b === selectedId;
    ctx.globalAlpha = matches(a) && matches(b) ? adjacent ? .95 : .5 : .12;
    ctx.lineCap = 'round';
    edgeLines.draw(ctx,edge,p,q,nodeScreenRadius(a),nodeScreenRadius(b),view.id);
    const chart=edgeChart(edge,edgeHistory.get(edge),p,q,nodeScreenRadius(a),nodeScreenRadius(b));
    if(chart)nextEdgeCharts.push(chart);
    // Only measured endpoint flows establish a traffic direction.
    for (const flow of trafficParticles(edge)) {
      const start = flow.a === edge.a ? p : q, end = flow.a === edge.a ? q : p;
      const count = Math.min(7, 1 + Math.ceil(Math.log10(1 + flow.actual_mbps * 100)));
      ctx.fillStyle = style.color;
      for (let i = 0; i < count; i++) {
        const progress = (particleTime / 2200 + i / count) % 1;
        const x = start.x + (end.x-start.x)*progress, y = start.y + (end.y-start.y)*progress;
        const angle = Math.atan2(end.y-start.y, end.x-start.x);
        ctx.save(); ctx.translate(x,y); ctx.rotate(angle);
        ctx.beginPath(); ctx.moveTo(4,0); ctx.lineTo(-3,-2.5); ctx.lineTo(-3,2.5); ctx.closePath(); ctx.fill(); ctx.restore();
      }
    }
    ctx.lineWidth = 1;

  }
  edgeCharts=nextEdgeCharts;
  if(hoverPoint&&!gesture&&time-lastEdgeHover>250){
    hoveredEdge=hit(hoverPoint)?null:hitEdge(hoverPoint,edgeCharts);
    if(hoveredEdge){edgeHover.show(hoveredEdge,hoverPoint,nodes,data,coordinatorContext);canvas.setAttribute('aria-describedby','edge-hover');}
    else{edgeHover.hide();canvas.removeAttribute('aria-describedby');canvas.style.cursor='grab';}
    lastEdgeHover=time;
  }
  const labelBoxes = [];
  for (const node of nodes.filter(node=>!geographic||node.geographic).sort((a, b) => Number(b.perspective) - Number(a.perspective) || Number(b.you) - Number(a.you))) {
    const p = screen(node), radius = nodeScreenRadius(node);
    const alpha = !matches(node) ? .12 : node.visibility === 'inventory' ? .5 : 1;
    ctx.globalAlpha = alpha;
    ctx.fillStyle=color(node);ctx.beginPath();ctx.arc(p.x,p.y,radius,0,Math.PI*2);ctx.fill();
    ctx.strokeStyle=node.visual.ring;ctx.lineWidth=2.5;ctx.setLineDash(node.visual.health==='unknown'?[3,4]:[]);
    ctx.beginPath();ctx.arc(p.x,p.y,radius+5,0,Math.PI*2);ctx.stroke();ctx.setLineDash([]);
    if(node.perspective){ctx.strokeStyle='#ecf1f5';ctx.beginPath();ctx.arc(p.x,p.y,radius+10,0,Math.PI*2);ctx.stroke();}
    ctx.lineWidth=1;
    if ((labels && matches(node)) || node.perspective || node.you) {
      ctx.font = '14px system-ui';
      const important = node.perspective || node.you;
      const text = ((layer==='ip'&&!allConnections)&&node.ip_hop ? 'IP hop '+node.ip_hop+' · '+label(node) : label(node)) + (node.you ? ' · You' : '');
      const textLimit=(layer==='ip'&&!allConnections)?140:180;
      const textWidth = Math.min(textLimit, ctx.measureText(text).width);
      const boxHeight = important ? node.test ? 55 : 40 : 20;
      const candidates = [
        {x:p.x-textWidth/2-5,y:p.y+radius+8},
        {x:p.x-textWidth/2-5,y:p.y-radius-boxHeight-8},
        {x:p.x+radius+10,y:p.y-boxHeight/2},
        {x:p.x-radius-textWidth-20,y:p.y-boxHeight/2}
      ].map(point=>({...point,w:textWidth+10,h:boxHeight}));
      const box = candidates.find(box=>box.x>=4 && box.y>=4 && box.x+box.w<=width-4 && box.y+box.h<=height-4 &&
        !labelBoxes.some(b=>box.x<b.x+b.w && box.x+box.w>b.x && box.y<b.y+b.h && box.y+box.h>b.y)) || (important ? candidates[0] : null);
      if (!box) continue;
      labelBoxes.push(box);
      ctx.fillStyle = '#0b1116dc';ctx.fillRect(box.x,box.y,box.w,box.h);
      ctx.textAlign = 'center'; ctx.fillStyle = '#d7e3eb';
      ctx.fillText(text, box.x+box.w/2, box.y+14, textLimit);
      if (important) {
        ctx.font = '12px system-ui'; ctx.fillStyle = '#bdcdd7';
        ctx.fillText((node.ip || '')+(!allConnections&&layer!=='ip'&&node.ip_hop ? ' · IP hop '+node.ip_hop : node.id===data.router?.mac ? ' · L2 bridge' : ''), box.x+box.w/2, box.y+30,textLimit);
        if (node.test) {
          ctx.fillStyle = node.test.status === 'passed' ? '#a6dfc4' : '#a5afba';
          ctx.fillText(node.test.transfer ? `${node.test.transfer.mbps.toFixed(1)} Mbps · ${node.test.status}` : 'Tests ' + node.test.status, box.x+box.w/2, box.y+45,textLimit);
        }
      }
    }
  }

  frames++;
  if (time - frameStamp > 1000) {
    $('fps').textContent = `${Math.round(frames * 1000 / (time - frameStamp))} FPS · ${nodes.length} particles`;
    frames = 0; frameStamp = time;
  }
  requestAnimationFrame(frame);
}
function pauseUI() {
  const reduced = motionPreference.matches;
  $('pause').textContent = reduced ? 'Reduced motion' : paused ? 'Resume' : 'Pause';
  $('pause').disabled = reduced;
  $('pause').setAttribute('aria-pressed', String(paused || reduced));
}
pauseUI();
$('pause').onclick = () => { paused = !paused; settings.paused=paused; pauseUI(); persistSettings(); };
$('labels').onchange = event => { labels = event.target.checked; persistSettings(); };
$('resolve-names').onchange = event=>{resolveNames=event.target.checked;persistSettings();};
$('fit').onclick = fit;
function zoomBy(factor){const before=zoom;zoom=Math.max(geographic?.08:.25,Math.min(64,zoom*factor));pan.x*=zoom/before;pan.y*=zoom/before;}
$('zoom-in').onclick=()=>zoomBy(1.7);
$('zoom-out').onclick=()=>zoomBy(1/1.7);
$('group-providers').onchange = event => {
  groupProviders=event.target.checked;persistSettings();
  if(!data)return;
  selectedId=null;detailSignature=null;nodes=[];positionCache.clear();sync();requestAnimationFrame(fit);
};
$('geographic').onchange = async event => {
  geographic = event.target.checked;geoRequested=false;persistSettings();
  if (!data) return;
  if (!geographic) {nodes=[];positionCache.clear();}
  detailSignature=null; sync(); requestAnimationFrame(fit);

};
$('all-connections').onchange=event=>{
  allConnections=event.target.checked;
  if(allConnections){lanOnly=false;$('lan-only').checked=false;}
  if(allConnections&&geographic){geographic=false;geoRequested=false;$('geographic').checked=false;nodes=[];positionCache.clear();}
  $('layer').disabled=allConnections;$('geographic').disabled=allConnections;$('group-providers').disabled=allConnections;
  persistSettings();clearEdgeHover();if(data){sync();requestAnimationFrame(fit);}
};
$('layer').onchange = event => {layer=event.target.value;persistSettings();view=null;if(data)sync();};
$('mine').onclick = () => { if (data) choose(data.viewer.id); };
$('search').oninput = event => { filter = event.target.value.toLowerCase(); renderList(); renderHistory(); };
$('repulsion').oninput = event => { $('force-value').value = event.target.value;persistSettings(); };
async function post(path, button) {
  button.disabled = true;
  try { const response = await fetch(path, {method: 'POST'}); if (!response.ok) throw Error('HTTP ' + response.status); }
  catch (error) { $('error').textContent = error.message; button.disabled = false; }
}
$('web-checks').onclick = () => post('/api/web-checks', $('web-checks'));
$('refresh').onclick = () => post('/api/refresh', $('refresh'));
async function poll() {
  try {
    const response = await snapshotFetch('/api/devices?resolve='+(resolveNames?'1':'0'), {signal: AbortSignal.timeout(7000)});
    if (!response.ok) throw Error('HTTP ' + response.status);
    const firstLoad=!data;
    data = await response.json(); sync();
    if(embeddedLAN&&firstLoad)requestAnimationFrame(fit);
  } catch (error) { $('error').textContent = 'Monitor unavailable: ' + error.message; }

}
async function pollArtefactContext(){
  if(!document.hidden){try{const r=await snapshotFetch('/api/distributed-tests',{cache:'no-store',signal:AbortSignal.timeout(4000)});if(!r.ok)throw Error();const result=await r.json();coordinatorContext={run:result.run,received:Date.now()};}catch{coordinatorContext=null;}}

}
pollVisible(pollArtefactContext,5000);pollVisible(poll,3000); requestAnimationFrame(frame);

$('lan-only').onchange=event=>{lanOnly=event.target.checked;persistSettings();clearEdgeHover();if(data)sync();};
