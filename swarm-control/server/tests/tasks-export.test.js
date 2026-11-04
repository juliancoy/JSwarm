import test from 'node:test';
import assert from 'node:assert/strict';
import {taskExport} from '../view/tasks-export.js';

test('task export retains hidden completed work, errors and retries and explains nested ETAs',()=>{
 const snapshot={sampled_at:200,errors:['source unavailable'],retry_jobs:[{task:'retry'}],tasks:[{id:'done',status:'completed'},{id:'run',children:[{id:'leaf',status:'running',sampled_at:195,execution_eta_seconds:60,eta_scope:'Checksum'}]}],planned:[{id:'later',status:'not scheduled'}]};
 const report=taskExport(snapshot,{statuses:['running']},205);
 assert.equal(report.tasks.length,2);
 assert.equal(report.tasks[1].children[0].eta_seconds,60);
 assert.match(report.tasks[1].children[0].eta_explanation,/Checksum ETA/);
 assert.equal(report.tasks[1].children[0].measurement_age_seconds,10);
 assert.deepEqual(report.retry_jobs,snapshot.retry_jobs);
 assert.deepEqual(report.errors,snapshot.errors);
 assert.equal(report.planned.length,1);
 assert.equal(snapshot.tasks[1].children[0].eta_explanation,undefined);
});

test('paused exports expire numerical estimates rather than repeat old timing claims',()=>{
 const report=taskExport({sampled_at:100,tasks:[{status:'running',sampled_at:100,execution_eta_seconds:60}],planned:[]},{paused:true},300);
 assert.equal(report.tasks[0].eta_seconds,null);
 assert.match(report.tasks[0].eta_explanation,/stale/);
 assert.equal(report.snapshot_age_seconds,200);
});
