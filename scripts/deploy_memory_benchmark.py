"""run.py lifecycle for the independent full-shard benchmark manager container."""
import hashlib,json,os,shutil,subprocess,tempfile
from pathlib import Path
NAME='swarm-control-memory-benchmark'


def manage(action,root):
    root=Path(root);home=Path.home()
    if action=='status':
        from scripts.benchmark_reference_memory import manage as status
        return status('status',root)
    row=subprocess.run(['docker','inspect',NAME],capture_output=True,text=True)
    if row.returncode==0:
        previous=json.loads(row.stdout)[0]
        if previous['Config'].get('Labels',{}).get('org.lifetech.service')!='memory-pressure-benchmark':raise RuntimeError('Foreign benchmark container name')
        if previous['State']['Running']:raise RuntimeError('Benchmark already running; use --memory-pressure-benchmark status')
        subprocess.run(['docker','rm',NAME],check=True,capture_output=True)
    base=json.loads(subprocess.check_output(['docker','inspect','swarm-control-alignment'],text=True))[0]['Image']
    from scripts import benchmark_reference_memory
    code=Path(benchmark_reference_memory.__file__).read_bytes()
    tag='lifetech/memory-benchmark:'+hashlib.sha256(base.encode()+code).hexdigest()[:16]
    from docker_utils.policy import build_image
    with tempfile.TemporaryDirectory(prefix='swarm-memory-benchmark-') as directory:
        path=Path(directory);(path/'benchmark_reference_memory.py').write_bytes(code)
        (path/'Dockerfile').write_text('FROM '+base+'\nHEALTHCHECK NONE\nCOPY benchmark_reference_memory.py /app/scripts/benchmark_reference_memory.py\nENTRYPOINT ["python3","-u","-m","scripts.benchmark_reference_memory"]\n')
        build_image(tag,path)
    args=['docker','run','-d','--name',NAME,'--init','--stop-timeout','120','--group-add',str(os.stat('/var/run/docker.sock').st_gid),'--network','host','--read-only','--cpus','1','--memory','512m','--memory-swap','512m','--pids-limit','128','--cap-drop','ALL','--security-opt','no-new-privileges','--user',str(os.getuid())+':'+str(os.getgid()),'--env','HOME='+str(home),'--env','PYTHONDONTWRITEBYTECODE=1','--shm-size','256m','--tmpfs','/tmp:rw,nosuid,nodev,noexec,size=32m,mode=1777','--label','organization.managed-by=run.py','--label','org.lifetech.service=memory-pressure-benchmark','--log-opt','max-size=5m','--log-opt','max-file=2']
    for source,readonly in ((root,False),(home/'.config/lifetech-launcher',True),(home/'.config/map-master/controller-signing.key',True),(home/'.local/state/swarm-control/submission',False),(Path('/var/run/docker.sock'),False)):
        args+=['--mount','type=bind,src='+str(source)+',dst='+str(source)+(',readonly' if readonly else '')]
    args+=[tag,'run','--root',str(root)]
    print(subprocess.check_output(args,text=True).strip())
    print('Benchmark manager started. Progress: python run.py --memory-pressure-benchmark status')
    return 0
