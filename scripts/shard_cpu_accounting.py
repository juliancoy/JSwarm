"""Read container CPU and memory accounting through enrolled launcher channels."""
import json
from pathlib import Path
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'swarm-control/server'))
from agent_client import AgentClient,CONFIG

REMOTE=r'''
import json,subprocess,sys,time
out={}
for name in json.loads(sys.argv[1]):
 p=subprocess.run(['docker','exec',name,'cat','/sys/fs/cgroup/cpu.stat'],capture_output=True,text=True,timeout=8)
 if p.returncode:continue
 try:
  v=dict(line.split() for line in p.stdout.splitlines())
  out[name]={'cpu_core_seconds':int(v['usage_usec'])/1e6,'cpu_throttled_seconds':int(v.get('throttled_usec',0))/1e6,'cpu_sampled_at':time.time(),'cpu_measurement_basis':'container cgroup v2 cumulative usage; includes all container processes'}
  from pathlib import Path
  root=Path('/sys/fs/cgroup')
  memory={k:int(v) for k,v in (line.split() for line in (root/'memory.stat').read_text().splitlines())}
  for filename,key in (('memory.current','memory_current_bytes'),('memory.peak','memory_peak_bytes'),('memory.swap.current','memory_swap_bytes')):
   try:out[name][key]=int((root/filename).read_text())
   except (OSError,ValueError):pass
  out[name].update(memory_anon_bytes=memory.get('anon'),memory_file_bytes=memory.get('file'),memory_shmem_bytes=memory.get('shmem'),memory_sampled_at=time.time(),memory_measurement_basis='container cgroup v2; file includes shmem, do not add them; shared cache owner charged separately')
 except (OSError,ValueError,KeyError):pass
print(json.dumps(out))
'''


def collect(reports):
    cfg=json.loads(CONFIG.read_text());groups={}
    for job,report in reports:
        if report.get('phase')=='running' and report.get('container') and report.get('host') in cfg['peers']:
            groups.setdefault(report['host'],[]).append((job,report['container']))
    def host_sample(entry):
        host,rows=entry
        client=AgentClient({'host':host,'agent_endpoint':'http://'+cfg['peers'][host]+':8790'})
        try:
            status=client.call('status',timeout=10)
            command=[sys.executable,str(ROOT/'scripts/swarm_command.py'),'--host',host,'--endpoint','http://'+cfg['peers'][host]+':8790','--machine',status['machine'],'--','python3','-c',REMOTE,json.dumps(list(dict.fromkeys(c for _,c in rows)))]
            data=json.loads(subprocess.check_output(command,text=True,timeout=35,stderr=subprocess.DEVNULL))
            return [(job,container,data[container]) for job,container in rows if container in data]
        except (OSError,ValueError,subprocess.SubprocessError):return []
        finally:client.close()
    if not groups:return []
    with ThreadPoolExecutor(max_workers=5) as pool:
        return [row for batch in pool.map(host_sample,groups.items()) for row in batch]
