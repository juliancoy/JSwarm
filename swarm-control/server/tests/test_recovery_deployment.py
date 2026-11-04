import json
from pathlib import Path
from unittest.mock import patch
import pytest
from recovery_deployment import deploy,FILES,SERVICES
from task_event_collector import collect_once
from task_events import EventStore


def test_collector_repairs_shadowed_record_without_executor_file_change(tmp_path):
    state=tmp_path/'state';(state/'distributed').mkdir(parents=True);(state/'tasks').mkdir()
    original={'id':'run','repositories':{'repo':'pinned'},'status':'passed'}
    (state/'distributed/run.json').write_text(json.dumps(original))
    (state/'tasks/run.json').write_text(json.dumps({'id':'run','status':'queued'}))
    store=EventStore(tmp_path/'journal')
    with patch('pathlib.Path.home',return_value=tmp_path):
        collect_once(store,state)
        # Reproduce an old journal already corrupted before the precedence fix.
        with store.connect() as db:db.execute("UPDATE runs SET category='registry',payload=? WHERE id='run'",(json.dumps({'id':'run'}),))
        collect_once(store,state)
    assert store.list_runs('application-tests')==[original]


@pytest.mark.parametrize('fail',[False,True])
def test_scoped_deployment_preserves_api_and_rolls_back_failed_health(tmp_path,fail):
    home=tmp_path;units=home/'.config/systemd/user';units.mkdir(parents=True)
    state=home/'.local/state/swarm-control';(state/'distributed').mkdir(parents=True)
    old=home/'.local/share/swarm-control/releases/old/swarm-control/server';old.mkdir(parents=True)
    source=home/'workspace';source.mkdir()
    for name in FILES:(old/name).write_text('old = True\n');(source/name).write_text('new = True\n')
    originals={name:'WorkingDirectory='+str(old)+'\nExecStart=python '+str(old/'server.py')+'\n' for name in SERVICES}
    for name,text in originals.items():(units/name).write_text(text)
    calls=[]
    def health(url):
        calls.append(url)
        if fail and len(calls)>1:raise OSError('unhealthy')
        return {'running':False}
    with patch('pathlib.Path.home',return_value=home),patch('recovery_deployment.snapshot',side_effect=health),patch('recovery_deployment.time.sleep'),patch('recovery_deployment.subprocess.run') as run:
        if fail:
            with pytest.raises(OSError):deploy(source)
        else:deploy(source)
    assert not (state/'distributed/DRAIN').exists()
    restarted=[call.args[0][3:] for call in run.call_args_list if 'restart' in call.args[0]]
    assert restarted and all(set(names)==set(SERVICES) for names in restarted)
    if fail:
        assert all((units/name).read_text()==text for name,text in originals.items())
    else:
        assert all('task-recovery-' in (units/name).read_text() for name in SERVICES)
