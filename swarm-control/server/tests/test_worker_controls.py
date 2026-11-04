import json
from pathlib import Path
from types import SimpleNamespace
import pytest
import worker_controls as controls


def test_unknown_hosts_and_actions_cannot_execute(monkeypatch):
    monkeypatch.setattr(controls,'enrolled',lambda:[{'host':'worker.local','local':False}])
    monkeypatch.setattr(controls,'capture',lambda *a,**k:pytest.fail('unexpected execution'))
    with pytest.raises(ValueError):controls.operate('other.local','stop')
    with pytest.raises(ValueError):controls.operate('worker.local','shell')


def test_remote_stop_and_resume_preserve_audio_and_never_restart_partial_jobs(tmp_path,monkeypatch):
    import subprocess
    monkeypatch.setattr(Path,'home',lambda:tmp_path)
    calls=[];running={'gpu','alignment','audio'}
    rows={
      'gpu':{'Config':{'Labels':{'org.lifetech.gpu-qc-service':'true','org.lifetech.worker':'true'}}},
      'alignment':{'Config':{'Labels':{'org.lifetech.worker':'true'}}},
      'audio':{'Config':{'Labels':{}}}}
    def run(args,**kwargs):
      calls.append(args)
      out='';rc=0
      if args[:3]==['docker','ps','-q']:out='\n'.join(i for i in running if rows[i]['Config']['Labels'].get('org.lifetech.worker')=='true')
      elif args[:2]==['docker','inspect']:out=json.dumps([rows[args[2]]])
      elif args[:2]==['docker','stop']:running.remove(args[-1])
      elif args[:2]==['docker','start']:running.add(args[-1])
      return SimpleNamespace(returncode=rc,stdout=out,stderr='')
    monkeypatch.setattr(subprocess,'run',run)
    exec('ACTION="stop"\n'+controls.SCRIPT,{})
    assert running=={'audio'}
    assert json.loads((tmp_path/'.config/lifetech-launcher/worker-policy.json').read_text())['mode']=='paused'
    assert all('pipewire.service' not in call for call in calls)
    exec('ACTION="resume"\n'+controls.SCRIPT,{})
    assert running=={'audio'}
    assert ['docker','start','alignment'] not in calls
    assert json.loads((tmp_path/'.config/lifetech-launcher/worker-policy.json').read_text())['mode']=='auto'


def test_pause_only_changes_admission_not_current_work(tmp_path,monkeypatch):
    import subprocess
    monkeypatch.setattr(Path,'home',lambda:tmp_path)
    calls=[]
    def run(args,**kw):
      calls.append(args);return SimpleNamespace(returncode=0,stdout='worker-id',stderr='')
    monkeypatch.setattr(subprocess,'run',run)
    exec('ACTION="pause"\n'+controls.SCRIPT,{})
    assert not any(call[0]=='systemctl' or call[:2]==['docker','stop'] for call in calls)


def test_controls_require_same_origin_lan_json_request():
    from resource_server import worker_request_allowed
    good={'Origin':'http://map.local','Host':'map.local','Content-Type':'application/json'}
    assert worker_request_allowed(good)
    assert not worker_request_allowed({**good,'Origin':'https://other.example'})
    assert not worker_request_allowed({**good,'Origin':''})
    assert not worker_request_allowed({**good,'Content-Type':'text/plain'})
    assert not worker_request_allowed({**good,'Host':'public.example','Origin':'http://public.example'})


def test_resume_uses_lease_supervisor_not_expired_container(monkeypatch):
    calls=[]
    monkeypatch.setattr(controls.subprocess,'run',lambda args,**kw:calls.append(args))
    controls.supervise_gpu('codecollective.local','resume')
    assert calls==[['systemctl','--user','start','lifetech-codecollective-gpu-qc.service']]
    controls.supervise_gpu('untrusted.local','stop')
    assert len(calls)==1
