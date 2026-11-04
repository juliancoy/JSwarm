export const percent=value=>Number.isFinite(value)?value.toFixed(1)+'%':'Unavailable';
export function cpuCoverage(data){
 const live=data.live_hosts??(data.hosts||[]).filter(h=>h.status==='live').length,total=data.total_hosts??data.hosts?.length??0;
 return `${live} / ${total} hosts measured · ${data.measured_logical_cpus??0} logical CPUs`+(live<total?' · Partial coverage':'');
}
export const cpuStatusLabel=status=>({permission_denied:'Permission denied',authentication_denied:'Agent authentication denied',warming:'Warming up',update_required:'Agent update required',stale:'Stale',unavailable:'Unavailable'}[status]||'Unavailable');
export function hostCpuDetail(host){
 const age=Number.isFinite(host.sample_age_seconds)?host.sample_age_seconds.toFixed(1)+'s ago':'age unknown';
 if(host.status!=='live'||!Number.isFinite(host.percent))return `${host.host} · ${cpuStatusLabel(host.status)} · ${age}`;
 const window=Number.isFinite(host.window_seconds)?host.window_seconds.toFixed(1)+'s':'unknown interval';
 return `${host.host} · ${percent(host.percent)} CPU · ${host.cpus} logical CPUs · ${window} average · ${age}`;
}
export function hostCpuContext(host){
 const phase={testing:'Running tests',preparing:'Preparing dependencies',waiting:'Waiting for work',idle:'No tests running',unknown:'Test activity unavailable'}[host.workload_phase]||'Test activity unavailable';
 const work=phase+(host.workload_phase==='testing'&&host.running_test_jobs?` · ${host.running_test_jobs} jobs`:'')+(host.scheduled_cpu_slots?` · ${host.scheduled_cpu_slots} scheduled CPU slots`:'');
 if(host.status!=='live')return work;
 return `${work} · I/O wait ${percent(host.cpu_iowait_percent)} · Stolen time ${percent(host.cpu_steal_percent)} · CPU pressure ${percent(host.cpu_pressure_some_percent)} (10s)`;
}
export function testCpuSummary(data){
 if(data.activity_status!=='available')return 'Test activity unavailable';
 if(!data.test_hosts)return 'No machines currently executing tests';
 return `Test-active machines: ${percent(data.active_test_percent)} · ${data.measured_test_hosts} / ${data.test_hosts} measured`;
}
