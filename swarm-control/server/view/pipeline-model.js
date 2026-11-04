import {deviceLevels,deviceColor} from './levels-model.js';
import {statusClass} from './task-display.js';
export const lanes=[['queued','Queued','Waiting for prerequisites or capacity'],['running','Running','Includes preparation and assigned work'],['completed','Complete','Newest completions first'],['failed','Needs attention','Failed, interrupted, partial, or unknown']];
export function laneFor(item){const state=statusClass(item.status).replace('status-','');return ['queued','running','completed'].includes(state)?state:'failed';}
export function resourceText(value){if(!value)return 'Not reported';return [value.cpus&&value.cpus+' CPU',value.memory_mb&&(value.memory_mb/1024).toLocaleString(undefined,{maximumFractionDigits:2})+' GiB RAM',value.sink].filter(Boolean).join(' · ')||'Not reported';}
export function facts(item){return [['History',item.history_reason],['Parent workflow',item.parent],['Job ID',item.job_id],['State',item.status+' · queue: '+item.queue_state],['Dependencies',item.dependencies?.length?item.dependencies.map(d=>typeof d==='string'?d:JSON.stringify(d)).join('\n'):(laneFor(item)==='queued'?'No explicit dependencies recorded. Awaiting admission or input.':'No explicit dependencies recorded.')],['Machine / sink',item.worker||'Unassigned'],['Execution runtime',item.runtime?.elapsed_seconds?(item.runtime.elapsed_seconds/60).toFixed(1)+' min':item.runtime?.timing_basis],['Run technologies',item.runtime?.technologies?.join(' · ')||'Not reported'],['Execution started',item.runtime?.started],['Execution finished',item.runtime?.finished],['Worker limits (recorded)',item.worker_resources?resourceText(item.worker_resources):'Not reported; assignment alone does not confirm a running worker'],['Placement budget',item.placement_resources?resourceText(item.placement_resources):'Not selected'],['Submission estimate',resourceText(item.submission_resources||item.resources)],['Peak RAM (measured)',item.peak_memory_bytes?(item.peak_memory_bytes/1024**3).toLocaleString(undefined,{maximumFractionDigits:2})+' GiB':null],['Memory profile',memoryProfileText(item)],['Configuration comparison',item.configuration_equivalent===false?'Alignment records differed from baseline':item.configuration_equivalent===true?'Alignment records matched baseline':null],['Execution observation',item.execution?.recorded_at?item.execution.phase+' · recorded '+new Date(item.execution.recorded_at*1000).toLocaleString()+' (not a live measurement)':null],['Work type',item.kind],['Generation',item.generation],['Reference',item.reference_mode],['Latest observation',item.updated?new Date(item.updated*1000).toLocaleString():'Not reported'],['Attempt count',item.attempts?.length],['Attempt history',item.attempts?.map(a=>a.parent+' · '+a.status+' · '+(a.worker||'Unassigned')).join('\n')],[item.detail_label||'Last queue note',item.detail]].filter(([,v])=>v!==undefined&&v!==null&&v!=='');}

export function currentItems(items){
 const parents=new Set(items.filter(i=>laneFor(i)!=='completed').map(i=>i.parent));
 if(!parents.size&&items.length)parents.add([...items].sort((a,b)=>b.updated-a.updated)[0].parent);
 return items.filter(i=>i.kind==='application-test'||parents.has(i.parent));
}

export function duration(seconds){if(!Number.isFinite(seconds))return 'Not recorded';if(seconds<60)return Math.round(seconds)+' sec';if(seconds<3600)return (seconds/60).toFixed(1)+' min';return (seconds/3600).toFixed(1)+' hr';}
export function runtimeProjection(item,items,now=Date.now()/1000){
 const runtime=item.runtime||{};
 if(item.status==='completed')return {label:'Finished',value:runtime.finished?new Date(runtime.finished).toLocaleString():'Completion recorded',note:'Recorded result'};
 if(['failed','interrupted','partial','unknown','error','timed_out'].includes(item.status))return {label:'Outcome',value:item.status.replaceAll('_',' '),note:item.history_reason||item.detail||'Execution ended without a verified result.'};
 if(item.status!=='running')return {label:'Projected finish',value:'Awaiting execution',note:'Start time depends on capacity and prerequisites.'};
 const e=item.runtime_estimate;
 if(!e)return {label:'Projected finish',value:'Learning runtime',note:'No compatible recorded execution samples yet.'};
 const note=e.samples+' samples · '+e.basis+' · '+e.confidence+' confidence'+(e.normalized?' · scaled by read count':'')+(e.overrun?' · longer than typical':'');
 if(!(runtime.elapsed_seconds>0))return {label:'Typical runtime',value:duration(e.typical_seconds),note:note+' · execution start not recorded'};
 return {label:'Estimated remaining',value:e.extrapolated?'Overrun · estimate uncertain':duration(e.remaining_seconds),note:note+' · '+(e.extrapolated?'Past observed samples; tail extrapolation':duration(e.remaining_low_seconds)+'–'+duration(e.remaining_high_seconds)+' empirical range')};
}
export function inspectorSections(item,items){
 const r=item.worker_resources||item.execution?.resources||item.placement_resources||item.submission_resources||{};
 const basis=item.worker_resources||item.execution?.resources?'Worker limit':item.placement_resources?'Placement budget':'Submission requirement';
 const projection=runtimeProjection(item,items);
 return [
  {title:'Runtime',rows:[[projection.label,projection.value,projection.note],['CPU core time',duration(item.runtime?.cpu_core_seconds),item.runtime?.cpu_measurement_basis||'Not measured'],['Average CPU cores',Number.isFinite(item.runtime?.average_cores)?item.runtime.average_cores.toFixed(2):'Not measured','Cumulative core-seconds / elapsed wall-seconds'],['CPU sampled',item.runtime?.cpu_sampled_at?new Date(item.runtime.cpu_sampled_at*1000).toLocaleTimeString():'Not recorded'],['Elapsed',duration(item.runtime?.elapsed_seconds),item.runtime?.timing_basis||(item.runtime?.elapsed_seconds?'Recorded execution time':'Execution time unavailable')],['Started',item.runtime?.started?new Date(item.runtime.started).toLocaleString():'Not recorded']]},
  {title:'Resources',metrics:[['CPU required',r.cpus?String(r.cpus)+' cores':'Unknown',basis],['RAM required',r.memory_mb?(r.memory_mb/1024).toFixed(2)+' GiB':'Unknown',basis]],rows:[['Current RAM',byteSize(item.execution?.memory_current_bytes),'Sampled container usage; includes RAM scratch'],['Peak RAM',byteSize(item.peak_memory_bytes||item.execution?.memory_peak_bytes),'Measured usage; separate from reserved RAM'],['Swap',byteSize(item.execution?.memory_swap_bytes),'Sampled container swap usage'],['Memory profile',memoryProfileText(item)||'Not enough matching measurements','Recommendations retain working-set headroom']]},
  ioSection(item),
  {title:'Execution',rows:[['Machine',item.worker||'Unassigned'],['Execution owner',item.execution?.execution_owner||'Not recorded'],['Reference location',item.execution?.reference_location||'Not recorded'],['Reference',item.reference_mode||'Not reported']]},
  {title:'Queue & provenance',disclosure:true,rows:facts(item).filter(([label])=>!['Machine / sink','Execution runtime','Run technologies','Execution started','Execution finished','Peak RAM (measured)','Reference'].includes(label))}
 ];
}

export function byteSize(bytes){if(!Number.isFinite(bytes)||bytes<0)return 'Not measured';if(bytes===0)return '0 B';const units=['B','KiB','MiB','GiB','TiB'];const power=Math.min(4,Math.floor(Math.log(bytes)/Math.log(1024)));return (bytes/1024**power).toLocaleString(undefined,{maximumFractionDigits:1})+' '+units[power];}
export function ioSection(item){const io=item.io||{};return {title:'Data movement',metrics:[
 ['Total read',byteSize(io.read_bytes),'Recorded payload bytes'],['Total written',byteSize(io.write_bytes),'Recorded payload bytes'],
 ['Network received',byteSize(io.network_read_bytes),'Recorded network payload'],['Network sent',byteSize(io.network_write_bytes),'Recorded network payload']],
 rows:[['Transfer state',io.state||'Not measured'],['Coverage',io.coverage||'No per-shard I/O accounting was retained.'],...(io.sampled_at?[['Last I/O observation',new Date(io.sampled_at*1000).toLocaleString(),'Counters advance after verified transfers; unfinished streams are excluded.']]:[])],
 charts:[{title:'Read by source',entries:io.read},{title:'Written by destination',entries:io.write}]};}

export const runTypes=[['genetic-qc','Genetic QC'],['mapping','Mapping'],['web','Web'],['other','Other']];
export function runType(item){return runTypes.some(([key])=>key===item.run_type)?item.run_type:item.kind==='paired-read-alignment'?'mapping':item.kind==='application-test'?'web':'other';}
export function completedNewestFirst(items){const time=item=>Number.isFinite(item.completed_at)?item.completed_at:Number.isFinite(Date.parse(item.runtime?.finished))?Date.parse(item.runtime.finished)/1000:Number(item.updated)||0;return items.filter(i=>laneFor(i)==='completed').sort((a,b)=>time(b)-time(a)||String(a.id).localeCompare(String(b.id)));}

export function usedGpu(item){return item.runtime?.gpu_used===true;}
export function technologyColor(label){if(/^GPU/i.test(label))return 'gpu';if(/vulkan|cuda|gasal/i.test(label))return 'accelerator';if(/CPU/i.test(label))return 'cpu';if(/FS-CACHE|NFS/i.test(label))return 'network';if(/shared|RAM/i.test(label))return 'memory';if(/bwa/i.test(label))return 'mapper';return 'other';}

export function executionGroup(item){
 const gpu=usedGpu(item)||String(item.worker||'').endsWith('#gpu')||item.execution?.sink==='gpu'||item.worker_resources?.sink==='gpu'||item.placement_resources?.sink==='gpu';
 if(!gpu)return {key:'cpu',label:'CPU',gpu:false};
 const devices=item.runtime?.gpu_devices||[];
 return {key:'gpu:'+JSON.stringify(devices),label:devices.length?'GPU · '+devices.map(name=>/^GPU-[a-f0-9-]+$/i.test(name)?name.slice(4,12):name).join(' / '):'GPU · device not reported',gpu:true};
}

export function arrivalPresentation(stats,{locale,timeZone}={}){
 if(!stats)return {value:'Unavailable',range:'',coverage:'Waiting for a current forecast.'};
 const date=value=>new Intl.DateTimeFormat(locale,{month:'short',day:'numeric',hour:'numeric',minute:'2-digit',timeZoneName:'short',...(timeZone?{timeZone}:{})}).format(new Date(value*1000));
 const arrival=stats.arrival_at;
 const remaining=stats.remaining_jobs||0,blocked=stats.blocked_jobs||0;
 const partial=!stats.complete_coverage;
 if(!remaining)return {value:'Submitted backlog complete',range:'',coverage:'Future shards and downstream work are not included.'};
 const valid=Number.isFinite(arrival);
 return {value:valid?(partial?'Partial arrival: ':'')+date(arrival):'Arrival unavailable',
  range:valid&&Number.isFinite(stats.arrival_low_at)&&Number.isFinite(stats.arrival_high_at)?'Observed range: '+date(stats.arrival_low_at)+' – '+date(stats.arrival_high_at):'',
  coverage:(stats.estimated_jobs??remaining).toLocaleString()+' of '+remaining.toLocaleString()+' remaining jobs estimated'+(blocked?' · '+blocked+' need attention':'')+(partial?' · '+(stats.unestimated_workflows?.length||0)+' workflow'+(stats.unestimated_workflows?.length===1?'':'s')+' incomplete':'')+'. Based on current throughput; future shards and downstream work excluded.'};
}


export function pipelineMachines(items,enrolled=[]){
 const names=[...enrolled,...items.map(item=>item.worker)].filter(name=>typeof name==='string'&&name).map(name=>name.split('#')[0]);
 return [...new Set(names)].sort();
}

export function memoryProfileText(item){const p=item.memory_profile;if(!p)return null;return p.samples+' verified samples · median '+p.median_peak_mb.toFixed(0)+' MiB · max '+p.max_peak_mb.toFixed(0)+' MiB'+(p.qualified?' · suggested ceiling '+p.recommended_limit_mb+' MiB':' · more qualification needed')+(p.shared_cache_separate?' · shared reference charged separately':'');}
