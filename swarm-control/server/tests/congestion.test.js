import test from 'node:test';
import assert from 'node:assert/strict';
import {linkSample,topPeers} from '../view/congestion-model.js';
const now=Date.now();
const data={interface_traffic:{wifi:{updated:new Date(now).toISOString(),rx_mbps:50,tx_mbps:90}},links:{wifi:{capacity_mbps:1000,rx_link_mbps:200,tx_link_mbps:100}}};
test('directional Wi-Fi ceilings are independent; traffic is not added across directions',()=>{const s=linkSample(data,'wifi',now);assert.equal(s.pressure,.9);assert.equal(s.rxCapacity,200);assert.equal(s.state,'Near link rate');});
test('stale data never appears as idle or healthy',()=>{const s=linkSample(data,'wifi',now+11000);assert.equal(s.rx,null);assert.equal(s.pressure,null);assert.equal(s.state,'No fresh sample');});
test('missing capacity does not imply zero utilization',()=>{const s=linkSample({...data,links:{}},'wifi',now);assert.equal(s.pressure,null);assert.equal(s.state,'Capacity unknown');});
test('device traffic only uses fresh capture and ranks actual rates',()=>{const d={devices:[{mac:'AA',hostname:'Laptop'}],link_traffic:{updated:new Date(now).toISOString(),peers:{aa:{rx_mbps:2,tx_mbps:1},bb:{rx_mbps:0,tx_mbps:1}}}};assert.equal(topPeers(d,now)[0].name,'Laptop');assert.equal(topPeers(d,now)[0].rate,3);assert.deepEqual(topPeers(d,now+11000),[]);});
