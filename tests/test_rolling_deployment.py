import json
from unittest.mock import Mock
import pytest

from scripts import deploy_swarm_inputs as deploy


def setup_deployment(monkeypatch,tmp_path):
    config=tmp_path/'peers.json'
    config.write_text(json.dumps({'name':'first','peers':{'first':'10.42.0.1','second':'10.42.0.2'}}))
    monkeypatch.setattr(deploy,'CONFIG',config)
    monkeypatch.setattr(deploy.Path,'home',lambda:tmp_path)
    updater=Mock(revision='new',generation=1)
    monkeypatch.setattr(deploy,'LauncherUpdates',__import__('types').SimpleNamespace(desired=lambda:updater))
    monkeypatch.setattr(deploy,'InputRuntimeRelease',lambda *a:Mock(revision='inputs',stage=Mock(return_value={'status':'current'})))
    clients=[]
    def client(host):
        value=Mock();value.call.return_value={'node':host['host'],'source_revision':'old','ready':True}
        clients.append(value);return value
    monkeypatch.setattr(deploy,'AgentClient',client)
    return updater,clients


def test_next_node_waits_for_verified_revision(monkeypatch,tmp_path):
    updater,clients=setup_deployment(monkeypatch,tmp_path)
    # A compatible old agent is usable for jobs, but does not complete an update.
    updater.ensure.side_effect=lambda host,client,status,**kw:status
    assert deploy.sweep() is False
    assert len(clients)==1
    assert clients[0].close.called
    report=json.loads((tmp_path/'.local/state/swarm-control/input-deployment.json').read_text())
    assert report['generation']==1
    assert [r['status'] for r in report['nodes']]==['pending','pending']
    assert report['nodes'][1]['reason']=='Waiting for first to become ready'


def test_rolls_in_enrolled_order_and_uses_existing_names(monkeypatch,tmp_path):
    updater,clients=setup_deployment(monkeypatch,tmp_path)
    order=[]
    def ensure(host,client,status,**kw):
        order.append(host['host'])
        assert kw=={'wait_timeout':900}
        assert len(clients)==len(order)
        return {**status,'source_revision':'new'}
    updater.ensure.side_effect=ensure
    assert deploy.sweep(wait_timeout=900) is True
    assert order==['first','second']
    assert all(c.close.called for c in clients)


def test_named_selection_does_not_contact_other_nodes(monkeypatch,tmp_path):
    updater,clients=setup_deployment(monkeypatch,tmp_path)
    updater.ensure.side_effect=lambda host,client,status,**kw:{**status,'source_revision':'new'}
    assert deploy.sweep(nodes=['second']) is True
    assert len(clients)==1
    assert updater.ensure.call_args.args[0]['host']=='second'


def test_existing_deployers_share_one_lock(monkeypatch,tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    import agent_updates
    monkeypatch.setattr(agent_updates.Path,'home',lambda:tmp_path)
    def competing_deployment():
        with agent_updates.deployment_lock():
            pytest.fail('Competing deployment acquired the lock')
    with ThreadPoolExecutor(max_workers=1) as executor:
        with agent_updates.deployment_lock():
            with pytest.raises(RuntimeError,match='Deployment locked by'):
                executor.submit(competing_deployment).result(timeout=5)
    with agent_updates.deployment_lock():pass


def test_stream_deploy_does_not_stage_the_rest_of_the_fleet_when_busy(monkeypatch,tmp_path):
    from scripts import deploy_stream_work as stream
    updater,clients=setup_deployment(monkeypatch,tmp_path)
    monkeypatch.setattr(stream,'CONFIG',deploy.CONFIG)
    monkeypatch.setattr(stream,'LauncherUpdates',__import__('types').SimpleNamespace(desired=lambda *a:updater))
    def client(host):
        value=Mock();value.call.return_value={'node':host['host'],'machine':'machine','ready':True,
            'source_revision':'old','worker_policy':{'accepting_jobs':True}}
        clients.append(value);return value
    monkeypatch.setattr(stream,'AgentClient',client)
    updater.ensure.side_effect=RuntimeError('Still draining')
    rows=stream.deploy([{'host':'first','machine':'machine'},{'host':'second','machine':'machine'}],progress=lambda _:None)
    assert len(rows)==len(clients)==1
    assert rows[0]['status']=='failed'
    assert updater.ensure.call_args.kwargs=={'drain':True,'wait_timeout':3600}
    updater.stage.assert_not_called()


def test_published_rollout_replaces_busy_nodes_without_adding_identities(monkeypatch,tmp_path):
    from types import SimpleNamespace
    updater,clients=setup_deployment(monkeypatch,tmp_path)
    monkeypatch.setattr(deploy,'LauncherUpdates',SimpleNamespace(published=lambda *a:updater))
    inputs=Mock(side_effect=AssertionError('Published node rollout must not create application work'))
    monkeypatch.setattr(deploy,'InputRuntimeRelease',inputs)
    def client(host):
        value=Mock();value.call.return_value={'node':host['host'],'source_revision':'old','ready':True,'active_commands':1 if host['host']=='first' else 0}
        clients.append(value);return value
    monkeypatch.setattr(deploy,'AgentClient',client)
    updater.ensure.side_effect=lambda host,client,status,**kwargs:{**status,'source_revision':'new'}
    assert deploy.sweep(published=tmp_path/'desired') is True
    assert updater.ensure.call_count==2
    assert updater.ensure.call_args.args[0]['host']=='second'
    report=json.loads((tmp_path/'.local/state/swarm-control/input-deployment.json').read_text())
    assert [row['node'] for row in report['nodes']]==['first','second']
    assert [row['status'] for row in report['nodes']]==['current','current']
    inputs.assert_not_called()


def test_launcher_deployment_does_not_execute_mutable_pager_provisioning(monkeypatch,tmp_path):
    updater,clients=setup_deployment(monkeypatch,tmp_path)
    updater.ensure.side_effect=lambda host,client,status,**kw:{**status,'source_revision':'new'}
    from scripts import swarm_paged_cache as pager
    path=tmp_path/'.local/state/swarm-control/submission/paged-cache/deployment.json'
    path.parent.mkdir(parents=True);path.write_text('{}')
    spec={'public':'artifact'};monkeypatch.setattr(pager,'current_plan',lambda:spec)
    provision=Mock(return_value={'first':{'ready':True},'second':{'ready':True}})
    monkeypatch.setattr(pager,'deploy_fleet',provision)
    assert deploy.sweep() is True
    provision.assert_not_called()
    report=json.loads((tmp_path/'.local/state/swarm-control/input-deployment.json').read_text())
    assert all('paged_cache' not in r for r in report['nodes'])


def test_deployment_entrypoint_resolves_workspace_modules_outside_checkout(tmp_path):
    import os,subprocess,sys
    from pathlib import Path
    entry=Path(deploy.__file__).resolve()
    code='import runpy,sys; runpy.run_path(sys.argv[1],run_name="deployment_import_check"); import scripts.swarm_paged_cache'
    env={k:v for k,v in os.environ.items() if k!='PYTHONPATH'}
    subprocess.run([sys.executable,'-c',code,str(entry)],cwd=tmp_path,env=env,check=True,capture_output=True,text=True,timeout=15)


def test_cli_defaults_to_published_release(monkeypatch,tmp_path):
    from contextlib import nullcontext
    monkeypatch.setattr(deploy,'RELEASES',tmp_path)
    (tmp_path/'desired').write_text('{}')
    monkeypatch.setattr(deploy.sys,'argv',['deploy_swarm_inputs.py'])
    monkeypatch.setattr(deploy,'deployment_lock',lambda **kwargs:nullcontext())
    sweep=Mock(return_value=True);monkeypatch.setattr(deploy,'sweep',sweep)
    deploy.main()
    sweep.assert_called_once_with(None,3600,tmp_path/'desired')


def test_status_checks_revision_and_identity_without_staging(monkeypatch,tmp_path,capsys):
    from types import SimpleNamespace
    updater,clients=setup_deployment(monkeypatch,tmp_path)
    monkeypatch.setattr(deploy,'LauncherUpdates',SimpleNamespace(desired=lambda:updater))
    monkeypatch.setattr(deploy.sys,'argv',['deploy_swarm_inputs.py','--status'])
    with pytest.raises(SystemExit) as stopped:deploy.main()
    assert stopped.value.code==1
    updater.ensure.assert_not_called()
    updater.stage.assert_not_called()
    assert all(c.close.called for c in clients)
    assert not any(row['current'] for row in json.loads(capsys.readouterr().out)['nodes'])


def test_unavailable_agent_recovers_through_verified_owner(monkeypatch,tmp_path):
    updater,clients=setup_deployment(monkeypatch,tmp_path)
    client=Mock();client.call.side_effect=ConnectionRefusedError('offline')
    monkeypatch.setattr(deploy,'AgentClient',lambda host:client)
    status={'node':'first','machine':'a'*64,'ready':False}
    updater.owner_status.return_value=status
    updater.ensure.return_value={**status,'source_revision':'new','ready':True}
    assert deploy.sweep(nodes=['first']) is True
    updater.owner_status.assert_called_once()
    assert updater.ensure.call_args.args[2]==status
    assert client.close.called


def test_normal_deployment_provisions_pager_from_published_snapshot(monkeypatch,tmp_path):
    updater,clients=setup_deployment(monkeypatch,tmp_path)
    updater.ensure.side_effect=lambda host,client,status,**kw:{**status,'source_revision':'new'}
    updater.components={'public-pager':{'sha256':'published'}}
    import deployment_components
    provision=Mock(return_value={'first':{'ready':True},'second':{'ready':True}})
    monkeypatch.setattr(deployment_components,'deploy',provision)
    assert deploy.sweep() is True
    provision.assert_called_once_with({'sha256':'published'},['first','second'])
    report=json.loads((tmp_path/'.local/state/swarm-control/input-deployment.json').read_text())
    assert all(r['paged_cache']['ready'] for r in report['nodes'])


def test_stream_deploy_can_skip_offline_preflight_before_replacement(monkeypatch,tmp_path):
    from scripts import deploy_stream_work as stream
    from types import SimpleNamespace
    updater,clients=setup_deployment(monkeypatch,tmp_path)
    monkeypatch.setattr(stream,'CONFIG',deploy.CONFIG)
    monkeypatch.setattr(stream,'LauncherUpdates',SimpleNamespace(desired=lambda *a:updater))
    def client(host):
        value=Mock()
        if host['host']=='first':value.call.side_effect=OSError('Offline')
        else:value.call.side_effect=[{'node':'second','machine':'machine','ready':True,'source_revision':'old','worker_policy':{'accepting_jobs':True}}, {'version':'launcher-stream-work.v1'}]
        clients.append(value);return value
    monkeypatch.setattr(stream,'AgentClient',client)
    updater.ensure.return_value={'source_revision':'new','ready':True,'worker_policy':{'accepting_jobs':True}}
    rows=stream.deploy([{'host':'first','machine':'machine'},{'host':'second','machine':'machine'}],progress=lambda _:None)
    assert [row['status'] for row in rows]==['failed','updated']
    assert updater.ensure.call_count==1 and updater.ensure.call_args.args[0]['host']=='second'


def test_removed_published_flag_is_rejected_before_deployment(monkeypatch):
    monkeypatch.setattr(deploy.sys,'argv',['deploy_swarm_inputs.py','--published'])
    sweep=Mock();monkeypatch.setattr(deploy,'sweep',sweep)
    with pytest.raises(SystemExit) as stopped:deploy.main()
    assert stopped.value.code==2
    sweep.assert_not_called()
