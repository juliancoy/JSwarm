"""Real Docker evidence for worker discovery and host/container singleton fencing."""
import json
from pathlib import Path
import subprocess
import threading
from types import SimpleNamespace
import uuid
from scripts.launcher_agent import Agent,launcher_lock

IMAGE='python:3.12-bookworm'


def test_worker_survives_launcher_close_and_is_rediscovered(tmp_path,monkeypatch):
    ident=uuid.uuid4().hex;name='genomics-'+ident
    monkeypatch.setenv('SWARM_RESOURCE_DIR',str(tmp_path/'resources'))
    (tmp_path/'resources').mkdir();(tmp_path/'resources/docker-workers').touch()
    def agent():
        a=Agent.__new__(Agent);a.root=tmp_path;a.config=tmp_path/'config.json';a.lock=threading.RLock()
        a.pending_resources={};a.busy=lambda:None;a.telemetry=SimpleNamespace(stopped=threading.Event())
        a.storage_stop=threading.Event();a.browser_timers={};a.browsers=set();a.jobs={}
        return a
    cid=subprocess.check_output(['docker','run','-d','--rm','--pull=never','--name',name,
        '--label','org.lifetech.worker=true','--label','org.lifetech.job='+ident,
        '--label','org.lifetech.group=restart-test','--cpus','1','--memory','64m','--pids-limit','32',
        '--network','none',IMAGE,'python','-c','import time; time.sleep(120)'],text=True).strip()
    try:
        before=json.loads(subprocess.check_output(['docker','inspect',cid],text=True))[0]['State']['StartedAt']
        a=agent();assert any(w['job']==ident for w in a.reconcile_resources())
        a.close()
        b=agent();assert b.dispatch('resource.renew',{'id':ident})['managed_by']=='docker'
        state=json.loads(subprocess.check_output(['docker','inspect',cid],text=True))[0]['State']
        assert state['Running'] and state['StartedAt']==before
        assert not list((tmp_path/'resources').glob('*.json'))
    finally:subprocess.run(['docker','rm','-f',cid],check=True,capture_output=True)


def test_docker_and_host_share_launcher_lock(tmp_path):
    path=tmp_path/'agent.lock';held=launcher_lock(path)
    code="import fcntl; f=open('/config/agent.lock','a+'); fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)"
    cmd=['docker','run','--rm','--pull=never','--network','none','--mount','type=bind,src='+str(tmp_path)+',dst=/config',IMAGE,'python','-c',code]
    try:
        r=subprocess.run(cmd,capture_output=True,text=True,timeout=30)
        assert r.returncode!=0 and 'BlockingIOError' in r.stderr
    finally:held.close()
    assert subprocess.run(cmd,capture_output=True,timeout=30).returncode==0


def test_real_supervisor_replaces_agent_and_adopts_same_docker_worker(tmp_path):
    """Isolated supervisor process replacement; no live node process is touched."""
    import hashlib,time
    from scripts import launcher_agent,launcher_peer_supervisor
    ident=uuid.uuid4().hex;name='genomics-'+ident
    worker=subprocess.check_output(['docker','run','-d','--pull=never','--name',name,'--label','org.lifetech.worker=true',
        '--label','org.lifetech.job='+ident,'--label','org.lifetech.group=restart-test','--cpus','1','--memory','64m',
        '--pids-limit','32','--network','none',IMAGE,'python','-c','import time; time.sleep(120)'],text=True).strip()
    sandbox=None
    try:
        initial=json.loads(subprocess.check_output(['docker','inspect',worker],text=True))[0]
        config=tmp_path/'config';config.mkdir();(config/'peers.json').write_text('{}')
        (config/'job.json').write_text(json.dumps({'job':ident,'container':name}))
        base=tmp_path/'base';base.mkdir()
        peer_source="""import json,os,time
from pathlib import Path
from launcher_agent import Agent,DockerWorkerJournal,launcher_lock
config=Path(os.environ['LIFETECH_PEER_CONFIG']);lock=launcher_lock(config.parent/'launcher-agent.lock')
a=Agent.__new__(Agent);a.worker_journal=DockerWorkerJournal(config.parent/'docker-worker-journal.sqlite')
a.recover_docker_workers()
value=a.worker_status(json.loads((config.parent/'job.json').read_text()))
value.update(pid=os.getpid(),folder=str(Path(__file__).parent))
(config.parent/'observed.json').write_text(json.dumps(value))
Path('/tmp/launcher-agent-busy.json').write_text(json.dumps({'jobs':['already-running'],'browsers':[],'transfers':25,'workers_survive_restart':True}))
Path('/tmp/launcher-agent-ready.json').write_text(json.dumps({'folder':str(Path(__file__).parent),'time':time.time()}))
while True:time.sleep(1)
"""
        files={n:'' for n in ('launcher_peers.py','launcher_discovery.py','launcher_registration.py','launcher_agent.py','launcher_registry.py')}
        files.update({'launcher_peers.py':peer_source,'launcher_agent.py':Path(launcher_agent.__file__).read_text()})
        for n,v in files.items():(base/n).write_text(v)
        (base/'launcher_peer_supervisor.py').write_text(Path(launcher_peer_supervisor.__file__).read_text())
        sandbox=subprocess.check_output(['docker','run','-d','--pull=never','--network','none','--memory','256m','--pids-limit','64',
            '--mount','type=bind,src='+str(tmp_path)+',dst=/fixture',
            '--mount','type=bind,src=/usr/bin/docker,dst=/usr/local/bin/docker,readonly',
            '--mount','type=bind,src=/var/run/docker.sock,dst=/var/run/docker.sock',
            '--env','LIFETECH_PEER_CONFIG=/fixture/config/peers.json',IMAGE,'python','/fixture/base/launcher_peer_supervisor.py'],text=True).strip()
        def await_observation(folder):
            deadline=time.monotonic()+35
            while time.monotonic()<deadline:
                try:
                    value=json.loads((config/'observed.json').read_text())
                    if value['folder']==folder:return value
                except (OSError,ValueError):pass
                time.sleep(.1)
            logs=subprocess.check_output(['docker','logs',sandbox],text=True)
            raise AssertionError('Replacement did not become ready: '+logs)
        first=await_observation('/fixture/base')
        files['launcher_peers.py']+='\n# validated next release\n'
        revision=hashlib.sha256(b''.join(files[n].encode() for n in files)).hexdigest()
        release=config/'agent-releases'/revision;release.mkdir(parents=True)
        for n,v in files.items():(release/n).write_text(v)
        (release/'manifest.json').write_text(json.dumps({n:hashlib.sha256(v.encode()).hexdigest() for n,v in files.items()}))
        (release.parent/'current').symlink_to(revision)
        second=await_observation('/fixture/config/agent-releases/'+revision)
        assert first['pid']!=second['pid']
        assert first['container_id']==second['container_id']==worker
        assert first['job']==second['job']==ident
        final=json.loads(subprocess.check_output(['docker','inspect',worker],text=True))[0]
        assert final['State']['Running'] and final['State']['StartedAt']==initial['State']['StartedAt']
    finally:
        for container in (sandbox,worker):
            if container:subprocess.run(['docker','rm','-f',container],capture_output=True,check=True)
