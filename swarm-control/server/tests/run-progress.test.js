import test from 'node:test';
import assert from 'node:assert/strict';
import {summarizeProgress} from '../view/run-progress.js';

test('aggregate includes every planned file, active job and queued file',()=>{
 const p=summarizeProgress({status:'running',task_catalog:['a','b','c','d'],pending_tasks:['d'],hosts:[{host:'one',jobs:{a:{status:'passed'},b:{status:'running'}}},{host:'two',jobs:{c:{status:'failed'}}}]});
 assert.equal(p.total,4);assert.equal(p.completed,2);assert.equal(p.percent,50);assert.equal(p.running,1);assert.equal(p.queued,1);assert.equal(p.failed,1);
});
test('reassigned jobs count once, and retry success replaces failed attempt',()=>{
 const p=summarizeProgress({status:'passed',task_catalog:['a'],hosts:[{host:'lost',jobs:{a:{status:'unavailable'}}},{host:'new',jobs:{a:{status:'passed'}}}]});
 assert.equal(p.total,1);assert.equal(p.completed,1);assert.equal(p.passed,1);assert.equal(p.unavailable,0);
});
test('requeued attempts stay queued, and coordinated per-node jobs remain distinct',()=>{
 const p=summarizeProgress({status:'running',task_catalog:['a'],pending_tasks:['a'],hosts:[{host:'lost',jobs:{a:{status:'unavailable'},p2p:{status:'passed'}}},{host:'new',jobs:{p2p:{status:'running'}}}]});
 assert.equal(p.total,3);assert.equal(p.queued,1);assert.equal(p.running,1);assert.equal(p.completed,1);
});
test('preparation stays indeterminate before the complete plan is known',()=>{
 const p=summarizeProgress({status:'preparing',hosts:[{host:'one',session:{verified:true},jobs:{p2p:{status:'queued'}}}]});
 assert.equal(p.total,null);assert.equal(p.percent,null);assert.equal(p.ready,1);
});
test('failure completion and unavailable coverage are not passing results',()=>{
 const p=summarizeProgress({status:'failed',task_catalog:['a','b','c'],hosts:[{host:'one',jobs:{a:{status:'passed'},b:{status:'timed_out'},c:{status:'unavailable'}}}]});
 assert.equal(p.percent,100);assert.equal(p.passed,1);assert.equal(p.failed,1);assert.equal(p.unavailable,1);assert.equal(p.terminal,true);
});
