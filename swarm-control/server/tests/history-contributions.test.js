import test from 'node:test';
import assert from 'node:assert/strict';
import {computerContributions,rankComputers,duration} from '../view/history-contributions.js';
test('computer rankings sum filtered runs without treating missing CPU data as zero',()=>{
 const rows=[{machines:[{name:'a',completed_tasks:3,executed_tasks:3,cpu_seconds:5,cpu_measured_tasks:2,estimated_cpu_slot_seconds:12},{name:'b',completed_tasks:4,executed_tasks:4,cpu_seconds:null}]},{machines:[{name:'a',completed_tasks:3,executed_tasks:3,cpu_seconds:2,cpu_measured_tasks:1}]}];
 const totals=computerContributions(rows);
 assert.equal(rankComputers(totals)[0].name,'a');assert.equal(rankComputers(totals,'cpu_seconds')[0].cpu_seconds,7);
 assert.equal(totals.find(row=>row.name==='b').cpu_seconds,null);
 assert.equal(totals.find(row=>row.name==='a').completed_tasks,6);
 assert.equal(computerContributions(rows.slice(0,1))[0].completed_tasks,3);
 assert.equal(duration(null),'Not recorded');assert.equal(duration(0),'0.0 s');
});
