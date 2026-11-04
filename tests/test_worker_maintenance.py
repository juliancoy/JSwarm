import json
import os
from pathlib import Path
import subprocess
import sys
from scripts.worker_maintenance import CHANGE


def test_drain_is_idempotent_and_resume_restores_preference(tmp_path):
    config=tmp_path/'.config/lifetech-launcher';config.mkdir(parents=True)
    (config/'peers.json').write_text(json.dumps({'name':'fixture'}))
    policy=config/'worker-policy.json';policy.write_text(json.dumps({'mode':'always'}))
    def change(action,node='fixture'):
        return subprocess.run([sys.executable,'-c',CHANGE],input=json.dumps({'node':node,'action':action,'reason':'test'}),
                              env={**os.environ,'HOME':str(tmp_path)},capture_output=True,text=True)
    assert change('drain').returncode==0
    original=(config/'maintenance.json').read_text()
    policy.write_text(json.dumps({'mode':'paused'}))
    assert change('drain').returncode==0
    assert (config/'maintenance.json').read_text()==original
    assert change('resume').returncode==0
    assert not (config/'maintenance.json').exists()
    assert json.loads(policy.read_text())=={'mode':'always'}
    assert list(config.glob('maintenance-completed-*.json'))
    assert change('drain',node='wrong').returncode!=0
    assert not (config/'maintenance.json').exists()
