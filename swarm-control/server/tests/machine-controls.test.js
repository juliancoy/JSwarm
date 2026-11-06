import test from 'node:test';
import assert from 'node:assert/strict';
import {machineStateTags,actionDisabled,machineActions} from '../view/machine-controls.js';
test('tags show observed pause, power and update state',()=>{
 const battery={connection:'live',policy:'auto',accepting_jobs:false,on_battery:true};
 assert.deepEqual(machineStateTags(battery).map(x=>x.label),['Paused','Policy · Automatic','On battery']);
 assert.equal(machineStateTags({...battery,update_phase:'starting'})[0].label,'Updating');
 assert.equal(machineStateTags({...battery,connection:'stale'})[0].label,'Node state stale');
});
test('mutations are disabled while unavailable or sending',()=>{
 assert.deepEqual(machineActions.map(a=>a[0]),['pause','update','resume','auto']);
 for(const [action] of machineActions){assert.equal(actionDisabled(action,{connection:'stale'}),true);assert.equal(actionDisabled(action,{connection:'live'},true),true);}
 assert.equal(actionDisabled('pause',{connection:'live',policy:'paused'}),true);
 assert.equal(actionDisabled('resume',{connection:'live',policy:'paused'}),false);
 assert.equal(actionDisabled('update',{connection:'live',update_phase:'starting'}),true);
});
