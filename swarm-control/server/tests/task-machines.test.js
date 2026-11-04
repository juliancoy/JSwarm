import test from 'node:test';
import assert from 'node:assert/strict';
import {machineJobs} from '../view/task-machines.js';

test('short names, mDNS names, case and trailing dots share one machine',()=>{
 const tasks=[{id:'run',name:'Work',children:['compai','compai.local','COMPAI.LOCAL.'].map((worker,id)=>({id:String(id),worker,status:'running'}))}];
 const machines=machineJobs(tasks);
 assert.equal(machines.length,1);
 assert.equal(machines[0].name,'compai');
 assert.equal(machines[0].jobs.length,3);
});
test('normalizes shared assignments before deduplication and retains different hosts',()=>{
 const machines=machineJobs([{id:'run',children:[{id:'a',worker:'latitude, latitude.local',status:'running'},{id:'b',worker:'compai.local,latitude.local',status:'running'}]}]);
 assert.equal(machines.length,2);
 const jobs=machines.find(m=>m.name==='latitude').jobs;
 assert.equal(jobs.length,2);
 assert.equal(jobs[0].shared,false);
 assert.equal(jobs[1].shared,true);
});
test('waiting aliases group with unassigned work',()=>{
 const machines=machineJobs([{id:'run',children:[{id:'a',worker:'waiting for worker'},{id:'b',worker:'unassigned'}]}]);
 assert.equal(machines.length,1);
 assert.equal(machines[0].name,'Unassigned');
});
