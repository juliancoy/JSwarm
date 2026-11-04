export const finite = value => typeof value === 'number' && Number.isFinite(value) && value >= 0;
export function linkSample(data, id, now = Date.now()) {
 const traffic=data.interface_traffic?.[id], link=data.links?.[id]||{};
 const age=now-Date.parse(traffic?.updated);
 const fresh=Number.isFinite(age)&&age>=-5000&&age<10000;
 const rx=fresh&&finite(traffic.rx_mbps)?traffic.rx_mbps:null;
 const tx=fresh&&finite(traffic.tx_mbps)?traffic.tx_mbps:null;
 const cap=direction=>{const v=link[direction+'_link_mbps']??link.capacity_mbps;return finite(v)&&v>0?v:null;};
 const rxCapacity=cap('rx'),txCapacity=cap('tx');
 const ratios=[rx!==null&&rxCapacity?rx/rxCapacity:null,tx!==null&&txCapacity?tx/txCapacity:null].filter(v=>v!==null);
 const pressure=ratios.length?Math.max(...ratios):null;
 return {rx,tx,rxCapacity,txCapacity,fresh,pressure,state:!fresh?'No fresh sample':pressure===null?'Capacity unknown':pressure>=.85?'Near link rate':pressure>=.5?'Elevated load':'Headroom at link rate'};
}
export function topPeers(data,now=Date.now()) {
 const capture=data.link_traffic;
 if(!capture||capture.capture_error||now-Date.parse(capture.updated)>10000||!Number.isFinite(Date.parse(capture.updated)))return [];
 return Object.entries(capture.peers||{}).map(([mac,p])=>({mac,name:data.devices?.find(d=>d.mac?.toLowerCase()===mac.toLowerCase())?.hostname||data.devices?.find(d=>d.mac?.toLowerCase()===mac.toLowerCase())?.ip||mac,rate:(finite(p.rx_mbps)?p.rx_mbps:0)+(finite(p.tx_mbps)?p.tx_mbps:0)})).sort((a,b)=>b.rate-a.rate).slice(0,5);
}
