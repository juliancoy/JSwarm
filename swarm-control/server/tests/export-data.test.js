import test from 'node:test';
import assert from 'node:assert/strict';
import { collectDashboardData } from '../view/export-data.js';

test('exports the entire network and all retained runs, not just the selected host', async () => {
 const requested=[];
 const read=async path=>{
  requested.push(path);
  if(path==='/api/resources')return {hosts:[{host:'one',performance:{schema_version:1}}]};
  if(path.startsWith('/api/devices'))return {devices:[{hostname:'one'},{hostname:'two'}],device_history:[{name:'old'}],link_traffic:{available:false}};
  if(path==='/api/distributed-tests')return {running:true,retention_days:30,run:{id:'new',hosts:[{host:'one',jobs:{a:{status:'running',logs:['tail']}}},{host:'two'}]},history:[{id:'new'},{id:'old'}]};
  if(path.endsWith('id=old'))return {run:{id:'old',status:'failed',source_sha256:'exact-hash'}};
  throw Error('Unexpected request');
 };
 const data=await collectDashboardData(read,{paused:true});
 assert.equal(data.complete,true);assert.equal(data.resources.hosts[0].performance.schema_version,1);assert.equal(data.network.devices.length,2);assert.equal(data.network.device_history.length,1);
 assert.deepEqual(data.application_tests.reports.map(run=>run.id),['new','old']);assert.equal(data.application_tests.reports[0].hosts.length,2);
 assert.equal(data.application_tests.reports[1].source_sha256,'exact-hash');assert.equal(data.application_tests.reports[0].hosts[0].jobs.a.logs[0],'tail');
 assert.equal(requested.includes('/api/distributed-tests?id=new'),false);assert.equal(data.browser.map_settings.paused,true);
});
test('missing sources are explicit in the JSON rather than silently omitted or treated as success', async () => {
 const data=await collectDashboardData(async path=>{if(path.startsWith('/api/devices'))throw Error('HTTP 503');return {run:null,history:[],running:false};});
 assert.equal(data.complete,false);assert.equal(data.network,null);assert.equal(data.collection_errors.length,1);assert.match(data.collection_errors[0].error,/503/);
});
