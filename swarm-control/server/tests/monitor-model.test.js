import test from 'node:test';
import assert from 'node:assert/strict';
import {currentPerformance,memorySeries,sortedRows} from '../view/monitor-model.js';
test('AMD cache events never become DRAM bandwidth and stale samples clear',()=>{
 const host={performance:{schema_version:1,sampled_at:100,memory:{cache:{'cache references':1000,'cache misses':100},dram_read_bytes_per_second:null}}};
 const rows=memorySeries(host,105);
 assert.equal(rows.find(r=>r.key==='cache misses').value,100);
 assert.equal(rows.find(r=>r.key==='dram_read_bytes_per_second').value,null);
 assert.equal(currentPerformance(host,116),null);
});
test('numeric values sort numerically and missing rows remain last in both directions',()=>{
 assert.deepEqual(sortedRows([[10],[2],[null]],0,false),[[2],[10],[null]]);
 assert.deepEqual(sortedRows([[10],[2],[null]],0,true),[[10],[2],[null]]);
});
test('future and explicit stale telemetry cannot look current',()=>{
 assert.equal(currentPerformance({performance:{schema_version:1,sampled_at:110}},100),null);
 assert.equal(currentPerformance({performance:{schema_version:1,sampled_at:100,stale:true}},105),null);
});

import {aggregateSwarm,SWARM} from '../view/monitor-model.js';
const sample=(host,cores,busy,total,available)=>({host,status:'live',sampled_at:100,cpus:cores,percent:busy,
 performance:{schema_version:1,sampled_at:100,memory:{total_bytes:total,available_bytes:available},processes:[{pid:1,name:'worker'}],
 filesystems:[{device:'disk',mount:'/one',type:'ext4',total_bytes:100,available_bytes:30},{device:'disk',mount:'/alias',type:'ext4',total_bytes:100,available_bytes:30}]}});
test('swarm aggregates capacities and weights CPU by cores; aliases do not inflate capacity',()=>{
 const result=aggregateSwarm([sample('small',2,100,100,25),sample('large',6,0,300,75)],105);
 assert.equal(result.host,SWARM);assert.equal(result.percent,25);assert.equal(result.cpus,8);
 assert.equal(result.performance.memory.total_bytes,400);assert.equal(result.performance.memory.available_bytes,100);
 assert.equal(result.performance.filesystems.length,2);assert.equal(result.performance.processes[0].name,'small · worker');
 assert.equal(result.performance.memory.dram_read_bytes_per_second,null);
});
test('stale nodes and duplicate identities cannot inflate fleet totals',()=>{
 const a=sample('a',2,50,100,25),stale={...sample('gone',100,100,10000,9000),sampled_at:10};
 const result=aggregateSwarm([a,a,stale],105);
 assert.equal(result.cpus,2);assert.equal(result.performance.memory.total_bytes,100);
 assert.equal(result.performance.limits.reporting_nodes,1);assert.equal(result.performance.limits.total_nodes,2);
 assert.match(result.scope,/1\/2 nodes/);assert.equal(aggregateSwarm([stale],105).status,'unavailable');
});
test('fleet I/O sums measured directions and leaves unmeasured GPU use unknown',()=>{
 const a=sample('a',2,50,100,25),b=sample('b',2,50,100,25);
 a.disks=[{name:'sda',status:'live',sampled_at:100,read_bytes_per_second:10,write_bytes_per_second:20}];
 b.disks=[{name:'sda',status:'live',sampled_at:100,read_bytes_per_second:30,write_bytes_per_second:null}];
 a.network_interfaces=[{name:'eth0',status:'live',sampled_at:100,rx_mbps:4,tx_mbps:1}];
 b.network_interfaces=[{name:'eth0',status:'live',sampled_at:100,rx_mbps:1,tx_mbps:4}];
 a.hardware={sampled_at:100,gpus:[{name:'unknown'}]};b.hardware={sampled_at:100,gpus:[{name:'measured',utilization_percent:50}]};
 const result=aggregateSwarm([a,b],105);
 assert.equal(result.totals.disk_read,40);assert.equal(result.totals.disk_write,20);
 assert.equal(result.totals.network_rx,5);assert.equal(result.totals.network_tx,5);
 assert.equal(result.totals.gpu_measured,1);assert.equal(result.totals.gpu_busy,.5);
 assert.equal(result.hardware.gpus.length,2);
});
