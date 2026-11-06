"""Read-only pipeline data owned by this node, never a coordinator mirror.

The pipeline joins this coordinator-owned queue with worker-owned API observations.
Private enrollment material is used only for local agent calls and never returned.
"""
import hashlib
import json
from pathlib import Path
import sqlite3
import time


def report_key(path):
    return hashlib.sha256(str(path).encode()).hexdigest() if path else None


def queue_snapshot(path, node):
    """Only queue-owned definitions, placement, dependencies and verified receipts.

    No worker telemetry database, remote files or node-status mirrors are read.
    """
    from task_pipeline import resources,run_type,runtime_summary,shard_title,failure_detail
    path=Path(path)
    if not path.is_file():raise FileNotFoundError('Coordinator queue unavailable')
    items=[]
    with sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True,timeout=2) as db:
        db.row_factory=sqlite3.Row
        task_column='task_id' if any(r[1]=='task_id' for r in db.execute('PRAGMA table_info(submissions)')) else 'NULL AS task_id'
        rows=db.execute('SELECT namespace,id,spec,state,host,created,updated,request,error,result,'+task_column+' FROM submissions ORDER BY created,id LIMIT 100001')
        for row in rows:
            if len(items)>=100000:raise ValueError('Pipeline queue envelope exceeded')
            spec=json.loads(row['spec']);request=json.loads(row['request'] or '{}');result=json.loads(row['result'] or '{}')
            request=request if isinstance(request,dict) else {};result=result if isinstance(result,dict) else {}
            submitted=resources(spec.get('resources'));placement=resources(request.get('resources')) if row['state'] in ('accepted','preparing') else None
            directory=request.get('directory')
            keys=[report_key(str(Path(directory)/name)) for name in ('execution.json','staging-execution.json')] if directory else []
            if request.get('report'):keys.append(report_key(request['report']))
            verified=bool(result.get('receipt_sha256') or result.get('primary_pair_accounting_verified') is True)
            items.append({'id':row['namespace']+'/'+row['id'],'job_id':row['id'],'parent':row['namespace'],
                'task_id':row['task_id'],'name':shard_title(spec,row['id'],row['namespace']),'shard':spec.get('shard'),'chromosome':spec.get('chromosome'),
                'region':spec.get('region'),'stage':spec.get('stage'),'kind':spec.get('kind','work item'),
                'run_type':run_type(spec,request,row['namespace']),'queue_state':row['state'],
                'status':('waiting_capacity' if request.get('admission_pending') else 'preparing') if row['state']=='accepted' else row['state'],
                'worker':row['host'],'dependencies':spec.get('depends_on',spec.get('dependencies',[])),
                'resources':placement or submitted,'submission_resources':submitted,'placement_resources':placement,
                'worker_resources':None,'execution':None,'runtime':{},'report_keys':keys,
                'output_verified':verified,'completed_at':row['updated'] if row['state']=='completed' else None,
                'work_units':result.get('pairs') or spec.get('reads'),'detail':row['error'] or failure_detail(spec,result,row['state']),'detail_label':'Coordinator queue note','created':row['created'],'updated':row['updated'],
                'generation':spec.get('generation'),'reference_mode':None,'queue_node':node,
                'validation_identity':hashlib.sha256(json.dumps(spec,sort_keys=True,separators=(',',':')).encode()).hexdigest() if row['namespace'].startswith('intel-routing-smoke:') else None})
    workflow_types={}
    for item in items:
        if item['run_type']!='other':workflow_types.setdefault(item['parent'],set()).add(item['run_type'])
    for item in items:
        types=workflow_types.get(item['parent'],set())
        if item['run_type']=='other' and len(types)==1:item['run_type']=next(iter(types))
    # Repeated validation runs remain one logical test with intact attempt history.
    groups={};other=[]
    for item in items:
        family='application-test' if item['kind']=='application-test' else 'intel-routing-smoke' if item['validation_identity'] else None
        if family:groups.setdefault((family,item['job_id'],item['validation_identity']),[]).append(item)
        else:other.append(item)
    for (family,job,identity),attempts in groups.items():
        latest=dict(attempts[-1]);latest['id']=family+'/'+(identity[:16]+'/' if identity else '')+job
        latest['attempts']=[{k:a.get(k) for k in ('id','parent','status','queue_state','worker','created','updated','detail')} for a in reversed(attempts)]
        other.append(latest)
    return {'node':node,'source':'coordinator-owned queue and verification receipts','sampled_at':time.time(),
            'items':other,'runtime_summary':runtime_summary(other)}
