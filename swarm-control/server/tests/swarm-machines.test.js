import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {machineCards,taskState} from '../view/swarm-machines.js';
const fixture=()=>({devices:[{hostname:'worker',address:'10.0.0.2',reachable:true}],run:{status:'running',hosts:[{host:'worker.local',address:'10.0.0.2',status:'running',jobs:[{name:'One',status:'running'},{name:'Two',status:'failed'},{name:'Three',status:'queued'}]}]}});
test('one card combines network and worker records and preserves mixed task counts',()=>{
 const cards=machineCards(fixture());assert.equal(cards.length,1);assert.equal(cards[0].state,'failed');assert.deepEqual(cards[0].counts,{running:1,failed:1,queued:1});assert.equal(cards[0].reachable,true);
});
test('idle or missing ping observations do not imply stopped or passed tasks',()=>{
 const data=fixture();data.devices[0].reachable=false;data.run.hosts[0].jobs=[];data.run.hosts[0].status='passed';const card=machineCards(data)[0];assert.equal(card.state,'idle');assert.equal(card.reachable,false);
 data.run.hosts[0].address='10.0.0.3';data.run.hosts[0].host='other.local';assert.equal(machineCards(data).length,2);
});
test('ended runs do not show queued or running work as live',()=>{
 assert.equal(taskState('queued','failed'),'not_run');assert.equal(taskState('running','stopped'),'stopped');assert.equal(taskState('preparing','running'),'running');assert.equal(taskState('timed_out','running'),'failed');assert.equal(taskState('cancelled','running'),'stopped');
});
test('machine fills retain the existing distributed-test palette',()=>{
 const old=readFileSync(new URL('../view/tests.css',import.meta.url),'utf8'),current=readFileSync(new URL('../view/swarm-status.css',import.meta.url),'utf8');
 for(const [state,selector] of [['running','.running,.transferring,.preparing'],['failed','.failed,.timed_out'],['passed','.passed'],['unavailable','.unavailable,.interrupted']]){
  const start=old.indexOf(selector+'{--status:');assert(start>=0);const color=old.slice(start).match(/--status:(#[a-f0-9]{6})/)[1];assert(current.includes(`[data-state=${state}]{--machine-color:${color}}`));
 }
});

test('unknown or unexecuted tasks are not reported as an entirely passed machine',()=>{
 const data=fixture();data.run.status='passed';data.run.hosts[0].status='passed';data.run.hosts[0].jobs=[{name:'Done',status:'passed'},{name:'Waiting',status:'queued'}];assert.equal(machineCards(data)[0].state,'not_run');
 assert.equal(taskState('new_unknown_status','running'),'unknown');
});
