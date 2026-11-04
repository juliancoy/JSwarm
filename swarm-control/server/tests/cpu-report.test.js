import test from 'node:test';
import assert from 'node:assert/strict';
import {cpuCoverage,hostCpuDetail,hostCpuContext,testCpuSummary} from '../view/cpu-report.js';
test('CPU reporting distinguishes missing readings, freshness and scope',()=>{
 assert.match(cpuCoverage({live_hosts:1,total_hosts:2,measured_logical_cpus:8}),/Partial coverage/);
 assert.match(hostCpuDetail({host:'node',status:'permission_denied',percent:null,sample_age_seconds:0}),/Permission denied/);
 assert.match(hostCpuDetail({host:'node',status:'warming',percent:null}),/Warming up/);
 assert.match(hostCpuDetail({host:'node',status:'live',percent:0,cpus:8,window_seconds:5,sample_age_seconds:2}),/0.0% CPU.*5.0s average.*2.0s ago/);
 assert.match(hostCpuContext({status:'live',workload_phase:'preparing',cpu_iowait_percent:12,cpu_steal_percent:0}),/Preparing dependencies.*I\/O wait 12.0%/);
 assert.match(testCpuSummary({activity_status:'available',test_hosts:0}),/No machines/);
 assert.match(testCpuSummary({activity_status:'available',test_hosts:2,measured_test_hosts:1,active_test_percent:80}),/80.0%.*1 \/ 2 measured/);
});
