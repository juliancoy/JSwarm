import test from 'node:test';
import assert from 'node:assert/strict';
import {hardwareLines,probeBrowserGPU} from '../view/hardware.js';
test('hardware distinguishes detection, runtime and validated kernel',()=>{
 const hardware={storage:{free_bytes:27*2**30,total_bytes:467*2**30},gpus:[{name:'GPU <script>',vendor:'NVIDIA',memory_total_mib:12288,utilization_percent:40}],cuda_container_runtime:false};
 let lines=hardwareLines(hardware);assert(lines[0].includes('27.0 GiB free'));assert(lines[1].includes('CUDA runtime missing'));assert(!lines.some(line=>line.includes('verified')));
 hardware.gpu_validation={status:'passed',verified_at:1};assert(hardwareLines(hardware).some(line=>line.includes('kernel verified')));
 hardware.stale=true;assert(!hardwareLines(hardware)[1].includes('40% GPU'));
});
test('unavailable browser GPU cannot pass a compute test',async()=>{
 assert.equal((await probeBrowserGPU(null)).status,'unavailable');
 assert.equal((await probeBrowserGPU({requestAdapter:async()=>null})).status,'unavailable');
});
