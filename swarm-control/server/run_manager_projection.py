"""Present delegated input execution inside its parent run, without double counts."""
from copy import deepcopy


def attach_delegated_runs(tasks, registered, aggregate):
    indexed={r['id']:r for r in registered};attached=set();out=[]
    for source in tasks:
        task=deepcopy(source)
        delegated_ids={j['job'] for j in task.get('delegation_jobs',[])}
        task['children']=[c for c in task.get('children',[]) if c['id'] not in delegated_ids]
        for ident in dict.fromkeys(task.get('delegated_runs',[])):
            child=indexed.get(ident)
            if not child:
                expected=[j for j in task.get('delegation_jobs',[]) if j['run']==ident]
                child={'id':ident,'name':'Streaming inputs','status':'waiting','children':[
                    {'id':j['job'],'name':'Streamed input','status':'waiting','worker':j['worker'],
                     'detail':'Waiting for executor status; completion has not been verified'} for j in expected
                ] or [{'id':ident+'-unknown','name':'Executor status unavailable','status':'waiting'}]}
            else:attached.add(ident)
            task['children'].append(deepcopy(child))
        if task.get('delegated_runs'):
            task['progress']=aggregate(task['children'])
            if task['progress']['completed']==task['progress']['total']:task['status']='completed'
            else:task['status']='active'
        out.append(task)
    return out,[r for r in registered if r['id'] not in attached]
