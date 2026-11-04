import {readEta} from './task-display.js';

export function taskExport(snapshot, view = {}, now = Date.now()/1000) {
 const observationTime=Math.max(snapshot.sampled_at||0,now);
 function annotate(row){
  const age=row.sampled_at==null?null:Math.max(0,observationTime-row.sampled_at);
  const prepared={...row,...((age!=null&&age>=90)||now-(snapshot.sampled_at||now)>=90?{runtime_stale:true}:{}),...(row.children?{children:row.children.map(annotate)}:{})};
  const estimate=readEta(prepared,observationTime);
  return {...prepared,eta_explanation:estimate.text,eta_seconds:Number.isFinite(estimate.seconds)?estimate.seconds:null,
   measurement_age_seconds:age};
 }
 return {
  schema_version:1,kind:'swarm-tasks-for-llm',exported_at:new Date(now*1000).toISOString(),
  scope:'All tasks and planned work in the displayed API snapshot, including completed and filtered-out tasks. Operational evidence only; no genomic files are read. Copied locally; no LLM service is contacted.',
  eta_notes:[
   'ETAs cover their labelled phase, not necessarily the entire workflow.',
   'Numerical estimates require a known total and fresh progress measurements; stalled counters and unknown totals do not support an ETA.',
   'Input bytes read are not verified results. Queued dependencies and remaining verification can prevent a workflow ETA.',
   'Snapshot and per-worker measurement timestamps are retained; paused or disconnected pages may contain old observations.'
  ],view, snapshot_age_seconds:Math.max(0,now-(snapshot.sampled_at||now)),
  ...snapshot,tasks:(snapshot.tasks||[]).map(annotate),planned:(snapshot.planned||[]).map(annotate)
 };
}
