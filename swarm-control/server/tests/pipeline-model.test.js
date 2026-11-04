import test from 'node:test';
import assert from 'node:assert/strict';
import {currentItems,facts} from '../view/pipeline-model.js';

test('latest passing tests stay visible with unresolved and running work',()=>{
 const passed={id:'test/a',kind:'application-test',parent:'fix',status:'completed',attempts:[{parent:'fix',status:'completed'},{parent:'old',status:'failed'}]};
 const unresolved={id:'test/b',kind:'application-test',parent:'old',status:'failed'};
 const running={id:'dna/shard',parent:'dna',status:'running'};
 const rows=[passed,unresolved,running];
 assert.deepEqual(currentItems(rows),rows);
 assert.ok(facts(passed).some(([label,value])=>label==='Attempt history'&&value.includes('old · failed')));
});

test('current workflows are recomputed after polling rather than accumulated',()=>{
 const old={id:'old/a',parent:'old',status:'failed'};
 const done={...old,status:'completed',updated:1};
 const next={id:'new/a',parent:'new',status:'running',updated:2};
 assert.deepEqual(currentItems([old]),[old]);
 assert.deepEqual(currentItems([done,next]),[next]);
});

import {completedNewestFirst,runType} from '../view/pipeline-model.js';
test('completed shards sort by recorded completion rather than shard ID or queue age',()=>{
 const old={id:'999',status:'completed',completed_at:10,updated:100};
 const recent={id:'001',status:'completed',completed_at:20,updated:20};
 const fallback={id:'fallback',status:'completed',updated:15};
 const running={id:'running',status:'running',updated:200};
 assert.deepEqual(completedNewestFirst([old,running,recent,fallback]),[recent,fallback,old]);
 assert.equal(runType({run_type:'genetic-qc'}),'genetic-qc');
 assert.equal(runType({kind:'paired-read-alignment'}),'mapping');
 assert.equal(runType({kind:'application-test'}),'web');
 assert.equal(runType({run_type:'unrecognized'}),'other');
});

import {runtimeProjection} from '../view/pipeline-model.js';
test('failed historical work shows its outcome instead of a projected finish',()=>{
 const result=runtimeProjection({status:'failed',history_reason:'Previous attempt; repaired sweep running'},[]);
 assert.equal(result.label,'Outcome');assert.equal(result.value,'failed');assert.match(result.note,/Previous attempt/);
});

import {usedGpu,technologyColor,executionGroup} from '../view/pipeline-model.js';
test('GPU sections separate execution devices without marking reservations as GPU work',()=>{
 const cpu={worker_resources:{sink:'cpu'},runtime:{gpu_used:false}};
 const integrated={runtime:{gpu_used:true,gpu_devices:['AMD Radeon Graphics']}};
 const discrete={execution:{sink:'gpu'},runtime:{gpu_used:true,gpu_devices:['RTX 3060']}};
 assert.equal(executionGroup(cpu).key,'cpu');
 assert.notEqual(executionGroup(integrated).key,executionGroup(discrete).key);
 assert.match(executionGroup(integrated).label,/AMD Radeon Graphics/);
 assert.equal(usedGpu({placement_resources:{sink:'gpu'}}),false);
 assert.equal(usedGpu(integrated),true);
 assert.equal(technologyColor('GPU quality control'),'gpu');
 assert.equal(technologyColor('FS-CACHE reference'),'network');
 assert.equal(technologyColor('Shared BWA index (host RAM)'),'memory');
});

import {arrivalPresentation} from '../view/pipeline-model.js';
test('arrival crosses midnight in the viewer timezone and states partial coverage',()=>{
 const at=Date.parse('2026-11-04T06:00:00Z')/1000;
 const row=arrivalPresentation({arrival_at:at,arrival_low_at:at-3600,arrival_high_at:at+3600,remaining_jobs:10,estimated_jobs:8,blocked_jobs:2,complete_coverage:false,unestimated_workflows:['blocked']},{locale:'en-US',timeZone:'America/New_York'});
 assert.match(row.value,/Partial arrival: Nov 4/);
 assert.match(row.value,/1:00/);
 assert.match(row.coverage,/8 of 10/);
 assert.match(row.coverage,/2 need attention/);
 assert.match(row.range,/Observed range/);
});
test('missing arrival stays unavailable and empty backlog is complete',()=>{
 assert.equal(arrivalPresentation({remaining_jobs:2,arrival_at:null,complete_coverage:false}).value,'Arrival unavailable');
 assert.equal(arrivalPresentation({remaining_jobs:0}).value,'Submitted backlog complete');
});


import {pipelineMachines} from '../view/pipeline-model.js';
test('idle enrolled nodes remain visible with no current jobs',()=>{
 assert.deepEqual(pipelineMachines([{worker:'compai.local#cpu'}],['julian-minipc.local','compai.local']),['compai.local','julian-minipc.local']);
 assert.deepEqual(pipelineMachines([],['julian-minipc.local']),['julian-minipc.local']);
 assert.deepEqual(pipelineMachines([{worker:'retired.local#gpu'},{worker:null}]),['retired.local']);
});

import {memoryProfileText} from '../view/pipeline-model.js';
test('memory profiling distinguishes recommendations from measured peaks and shared RAM',()=>{
 const item={memory_profile:{samples:5,median_peak_mb:275,max_peak_mb:438,qualified:true,recommended_limit_mb:896,shared_cache_separate:true}};
 assert.match(memoryProfileText(item),/5 verified samples/);
 assert.match(memoryProfileText(item),/suggested ceiling 896 MiB/);
 assert.match(memoryProfileText(item),/shared reference charged separately/);
 assert.equal(memoryProfileText({}),null);
});
