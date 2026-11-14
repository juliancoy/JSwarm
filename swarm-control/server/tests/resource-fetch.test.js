import test from 'node:test';
import assert from 'node:assert/strict';
import {readResourceSnapshots} from '../view/resource-fetch.js';
test('one failed endpoint retains its snapshot without discarding other readings',async()=>{
 const old={hosts:[{host:'one',sampled_at:10}]};
 const result=await readResourceSnapshots(async url=>{if(url==='/api/devices?resolve=0')throw Error('timeout');return {hosts:[{host:'one',sampled_at:20}]};},{network:{devices:[{ip:'one'}]},metrics:old});
 assert.deepEqual(result.failed,['network']);assert.equal(result.snapshots.metrics.hosts[0].sampled_at,20);assert.equal(result.snapshots.network.devices[0].ip,'one');
});
test('total outage preserves original timestamps and authentication loss rejects',async()=>{
 const previous={metrics:{hosts:[{host:'one',sampled_at:10}]}};
 const result=await readResourceSnapshots(async()=>{throw Error('offline');},previous);
 assert.equal(result.snapshots.metrics,previous.metrics);assert.equal(result.failed.length,3);
 await assert.rejects(readResourceSnapshots(async()=>{throw Object.assign(Error('auth'),{status:401});},previous),{status:401});
});
