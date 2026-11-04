import test from 'node:test';
import assert from 'node:assert/strict';
import {throughputSources} from '../view/throughput-model.js';
const now=Date.now();
const data={devices:[{mac:'aa',hostname:'Laptop'}],remote_ips:{'1.1.1.1':{ptr:'one.one.one.one',organization:'Cloudflare'}},link_traffic:{updated:new Date(now).toISOString(),sample_seconds:1,flows:{records:[{device_mac:'aa',remote_ip:'1.1.1.1',download_mbps:8,upload_mbps:1},{device_mac:'bb',remote_ip:'1.1.1.1',download_mbps:2,upload_mbps:3},{device_mac:'bb',remote_ip:'8.8.8.8',download_mbps:0,upload_mbps:1}]}}};
test('download source ranking aggregates remote senders without confusing provider with application',()=>{const m=throughputSources(data,'download','',now);assert.equal(m.total,10);assert.equal(m.sources.length,1);assert.equal(m.sources[0].share,1);assert.equal(m.sources[0].name,'one.one.one.one');assert.equal(m.rows[0].provider,'Cloudflare');assert.equal(m.rows[0].deviceName,'Laptop');});
test('upload source ranking aggregates devices; shares match the selected direction',()=>{const m=throughputSources(data,'upload','',now);assert.equal(m.total,5);assert.equal(m.sources[0].id,'bb');assert.equal(m.sources[0].share,.8);});
test('device filter changes share denominator and never uses NIC totals',()=>{const m=throughputSources(data,'download','aa',now);assert.equal(m.total,8);assert.equal(m.allTotal,10);assert.equal(m.sources[0].share,1);});
test('stale and failed capture do not show historical counters as live rates',()=>{assert.equal(throughputSources(data,'download','',now+15000).total,null);assert.equal(throughputSources({...data,link_traffic:{...data.link_traffic,capture_error:'offline'}},'download','',now).fresh,false);});
test('fresh zero flows means no observed rate, not missing sample',()=>{const m=throughputSources({...data,link_traffic:{...data.link_traffic,flows:{records:[]}}},'download','',now);assert.equal(m.total,0);assert.equal(m.fresh,true);});
test('process group counts shared ownership once and retains unattributed traffic',()=>{
 const records=data.link_traffic.flows.records.map((r,i)=>i? r:{...r,recent_processes:[{name:'python3',pid:1},{name:'python3',pid:2}]});
 const m=throughputSources({...data,link_traffic:{...data.link_traffic,flows:{records}}},'download','',now,'process');
 assert.equal(m.total,10);assert.equal(m.sources[0].rate,8);assert.equal(m.sources.reduce((sum,r)=>sum+r.share,0),1);assert.match(m.sources[0].name,/python3/);
});
test('script filenames label process groups without claiming transferred filenames',()=>{
 const records=[{...data.link_traffic.flows.records[0],recent_processes:[{name:'python3',pid:12}]}];
 const resources={status:'live',sampled_at:Date.now()/1000,processes:{'12':{script:'/work/recovery.py'}}};
 const m=throughputSources({...data,process_resources:resources,link_traffic:{...data.link_traffic,flows:{records}}},'download','',now,'process');
 assert.match(m.sources[0].name,/recovery\.py/);assert.equal(m.total,8);
});
test('uplink ledger includes unknown owners without adding duplicate client-side flows',()=>{
 const uplink_flows={records:[{device_mac:'uplink-unattributed',local_ip:'10.0.0.1',remote_ip:'1.1.1.1',upload_mbps:2,download_mbps:3}]};
 const value={...data,link_traffic:{...data.link_traffic,uplink_flows}};
 const m=throughputSources(value,'upload','',now,'process','uplink');
 assert.equal(m.total,2);assert.equal(m.rows[0].deviceName,'Unattributed uplink');
 assert.equal(throughputSources(value,'upload','',now).total,5);
 assert.equal(throughputSources(data,'upload','',now,'sender','uplink').fresh,false);
});
