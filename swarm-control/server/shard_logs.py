"""On-demand, bounded log tails for observed shard workers."""
import json
import re
from agent_client import CONFIG
from interface_metrics import capture

SCRIPT = r'''
import json,subprocess,threading
name=CONTAINER
row=json.loads(subprocess.check_output(['docker','inspect',name],text=True,timeout=5))[0]
labels=row.get('Config',{}).get('Labels') or {}
if labels.get('org.lifetech.worker')!='true':raise ValueError('Not a swarm worker')
if row.get('HostConfig',{}).get('LogConfig',{}).get('Type')=='none':
 print(json.dumps({'available':False,'message':'Docker logging is disabled for this worker.'}))
else:
 p=subprocess.Popen(['docker','logs','--timestamps','--tail','200',row['Id']],stdout=subprocess.PIPE,stderr=subprocess.STDOUT)
 timer=threading.Timer(5,p.kill);timer.start()
 try:
  raw=p.stdout.read(65537)
  if len(raw)>65536:p.kill()
  code=p.wait(timeout=2)
 finally:
  timer.cancel()
  if p.poll() is None:p.kill();p.wait()
  p.stdout.close()
 print(json.dumps({'available':code==0 or len(raw)>65536,'text':raw[:65536].decode('utf-8','replace'),
  'truncated':len(raw)>65536,'running':bool(row['State']['Running']),
  'container':row['Id'],'message':'Log read unavailable or timed out.' if code and len(raw)<=65536 else ''}))
'''


def tail(ident, snapshot=None, reader=None):
    if not isinstance(ident,str) or not ident or len(ident)>2048:raise ValueError('Invalid shard')
    if snapshot is None:
        from pipeline_nodes import snapshot
    item=next((row for row in snapshot()['items'] if row['id']==ident),None)
    if item is None:raise ValueError('Unknown shard')
    host=(item.get('worker') or '').split('#',1)[0]
    execution=item.get('execution') or {}
    container=execution.get('container') or item.get('container')
    if not host or not container:return {'available':False,'message':'No Docker worker is recorded for this shard yet.'}
    config=json.loads(CONFIG.read_text())
    if host not in config['peers'] or not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}',container):
        raise ValueError('Invalid recorded worker')
    try:
        return (reader or capture)({'host':host,'local':host==config['name']},
            SCRIPT.replace('CONTAINER',repr(container)),timeout=10)
    except Exception:
        return {'available':False,'message':'Worker logs unavailable. The node may be offline or the container retired.'}
