const problems=new Set(['failed','timed_out','unavailable','interrupted']);
const ended=new Set(['partial','failed','passed','stopped','interrupted','timed_out','unavailable']);
const clean=value=>String(value??'').replace(/\x1b\[[0-?]*[ -/]*[@-~]/g,'').trim();
const lines=value=>Array.isArray(value)?value.map(clean).filter(Boolean):[];
function recordedEvidence(job){
 const details=lines(job.failure_details),logs=lines(job.logs),source=details.length?details:logs;
 const specific=/AssertionError|(?:Type|Reference|Syntax|Import|ModuleNotFound|Runtime|Value|Key)Error|Error:|error TS\d|fatal:|Cannot |cannot |Unable |unable |No space left|Permission denied|ERR!|ECONN|ENOTFOUND|EAI_AGAIN|Image ID verification|failed to|Timed? ?out|Timeout/i;
 let index=source.findIndex(line=>specific.test(line));
 if(index<0)index=source.findIndex(line=>/^(?:not ok\b|FAIL(?:ED)?\b|ERROR\b|E\s+)/.test(line));
 const diagnostic=index>=0;
 if(index<0)index=0;
 return {diagnostic,reason:source[index]||null,excerpt:source.slice(Math.max(0,index-2),Math.max(0,index-2)+20),
  source:details.length?'Recorded failure details':'Retained log tail'};
}
function category(text,fallback){
 if(/Cannot resolve LAN address|ENOTFOUND|EAI_AGAIN|Name or service not known|Temporary failure in name resolution/i.test(text))return 'Host / DNS resolution';
 if(/Image ID verification failed|required image.*(?:differs|not distributed)/i.test(text))return 'Image verification';
 if(/No space left|ENOSPC|disk full/i.test(text))return 'Storage';
 if(/Permission denied|EACCES|EPERM/i.test(text))return 'Permissions';
 return fallback;
}
export function summarizeFailure(run){
 if(!run)return {headline:'No run selected',incidents:[],counts:{},blocked:0,notExecuted:0};
 const incidents=[],counts={suiteFailures:0,unavailable:0,timeouts:0,interrupted:0,stopped:0};let blocked=0,notExecuted=0;
 const error=clean(run.error);
 if(error)incidents.push({key:'coordinator',kind:category(error,run.status==='stopped'?'Stopped by request':run.status==='interrupted'?'Interrupted':'Coordinator / preparation'),
  reason:error,phase:run.preparation||run.image_progress?.phase||'Coordinator',evidence:[error],evidenceSource:'Recorded run error',affected:[],coordinator:true});
 for(const host of run.hosts||[]){
  const hostError=clean(host.error)||clean(host.session?.error);
  if(hostError&&hostError!==error)incidents.push({key:'host:'+host.host,kind:category(hostError,'Host / launcher'),reason:hostError,
   phase:host.exclusion_stage||host.session?.status||host.status||'Unknown',evidence:[hostError],evidenceSource:'Recorded machine error',affected:[{host:host.host}],host:host.host});
  for(const [id,job]of Object.entries(host.jobs||{})){
   const evidence=recordedEvidence(job),ownEvidence=lines(job.failure_details).length>0||Boolean(job.error)||evidence.diagnostic||(job.test_counts?.failed||0)>0;
   if(job.status==='reused'){counts.reused=(counts.reused||0)+1;continue;}
   if(job.not_executed){notExecuted++;continue;}
   if(job.status==='stopped'){counts.stopped++;continue;}
   if(ended.has(run.status)&&!job.started&&['queued','running','preparing','transferring'].includes(job.status)){notExecuted++;continue;}
   if(!problems.has(job.status))continue;
   // A coordinator shutdown propagates infrastructure status to pending suites.
   // Keep this impact separate from recorded suite-level failure evidence.
   if(error&&job.phase==='infrastructure'&&!ownEvidence){blocked++;continue;}
   if(hostError&&!job.started&&!ownEvidence){blocked++;continue;}
   if(job.status==='timed_out')counts.timeouts++;
   else if(job.status==='unavailable')counts.unavailable++;
   else if(job.status==='interrupted')counts.interrupted++;
   else counts.suiteFailures++;
   const explicit=clean(job.error);
   const reason=explicit||evidence.reason||(job.status==='timed_out'?'Suite timed out; no specific error retained.':job.status==='unavailable'?'Worker unavailable; no specific error retained.':job.status==='interrupted'?'Execution interrupted; no specific error retained.':'Failure recorded without an error excerpt.');
   const kind=category(reason,job.phase==='prepare'?'Dependency preparation':job.phase==='test'?'Suite execution':job.status==='timed_out'?'Timeout':job.status==='unavailable'?'Worker unavailable':job.status==='interrupted'?'Interrupted':'Suite failure');
   const key=[kind,job.phase||'',reason].join('|'),existing=incidents.find(i=>i.key===key);
   const target={host:host.host,suite:id,name:job.name||id,exitCode:job.exit_code,status:job.status};
   if(existing)existing.affected.push(target);
   else incidents.push({key,kind,reason,phase:job.phase||'Not recorded',evidence:explicit?[explicit,...evidence.excerpt]:evidence.excerpt,
    evidenceSource:evidence.source,affected:[target],host:host.host,suite:id,exitCode:job.exit_code});
  }
 }
 const headline=incidents[0]?.reason||(run.status==='partial'?'Verified workers completed; excluded nodes or missing peer coverage are reported below.':run.status==='passed'?(counts.reused?'Passing coverage retained; '+counts.reused+' unchanged suites reused.':'All recorded suites passed.'):run.status==='stopped'?'Run stopped; this is not a passing or failing test result.':run.status==='interrupted'?'Run interrupted; no specific cause retained.':run.status==='failed'?'Run failed; no specific error retained.':'No failure evidence recorded so far.');
 return {headline,incidents,counts,blocked,notExecuted};
}
