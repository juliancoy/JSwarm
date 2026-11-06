import json
import subprocess
import sys
from unittest.mock import Mock
import pytest
import machine_controls as controls


def test_node_states_are_observations_not_requested_actions():
    value=controls.project('node',{'node':'node','worker_policy':{'mode':'auto','accepting_jobs':False,'on_battery':True,'reason':'Saving battery'},'update':{'phase':'starting'},'ready':False})
    assert value['policy']=='auto' and value['accepting_jobs'] is False
    assert value['on_battery'] is True and value['update_phase']=='starting'
    with pytest.raises(ValueError):controls.project('other',{'node':'node'})


def test_policy_changes_are_node_bound_and_resume_previous_preference(tmp_path):
    root=tmp_path/'config';root.mkdir();config=root/'peers.json';config.write_text('{"name":"node"}')
    policy=root/'worker-policy.json';policy.write_text('{"mode":"always"}')
    import hashlib,os
    machine=hashlib.sha256(__import__('pathlib').Path('/etc/machine-id').read_bytes()).hexdigest()
    env={**os.environ,'LIFETECH_PEER_CONFIG':str(config)}
    def change(action,node='node'):
        return subprocess.run([sys.executable,'-c',controls.POLICY_CHANGE],input=json.dumps({'node':node,'machine':machine,'action':action}),text=True,capture_output=True,env=env)
    assert change('pause').returncode==0
    assert change('pause').returncode==0
    assert json.loads(policy.read_text())=={'mode':'paused','resume_mode':'always'}
    assert change('resume').returncode==0
    assert json.loads(policy.read_text())['mode']=='always'
    assert change('auto').returncode==0
    assert json.loads(policy.read_text())['mode']=='auto'
    previous=policy.read_bytes()
    assert change('pause','wrong-node').returncode!=0
    assert policy.read_bytes()==previous


def test_unenrolled_and_arbitrary_actions_never_open_a_node_connection(tmp_path,monkeypatch):
    config=tmp_path/'config';config.write_text('{"name":"master","peers":{"node":"10.0.0.2"}}')
    monkeypatch.setattr(controls,'CONFIG',config)
    client=Mock();monkeypatch.setattr(controls,'AgentClient',client)
    with pytest.raises(ValueError):controls.request('http://attacker','pause')
    with pytest.raises(ValueError):controls.request('node','shell')
    client.assert_not_called()


def test_action_uses_selected_nodes_command_api_and_reports_uncertain_timeout(monkeypatch):
    import task_machines
    host={'host':'node','agent_endpoint':'http://10.0.0.2:8790'}
    client=Mock();client.call.return_value={'node':'node','machine':'machine'}
    monkeypatch.setattr(controls,'AgentClient',Mock(return_value=client))
    command=Mock(side_effect=TimeoutError());monkeypatch.setattr(task_machines,'command',command)
    monkeypatch.setattr(controls,'CACHE',{'node':{'value':{},'action':None}})
    controls.execute(host,{},'pause')
    assert json.loads(command.call_args.args[2])['node']=='node'
    assert controls.CACHE['node']['action']['status']=='unconfirmed'
    assert command.call_count==1
    client.close.assert_called_once()


def test_update_payload_uses_only_authoritative_publication(monkeypatch):
    import agent_updates
    release=Mock(supervisor_source='supervisor',revision='published',files={'pinned':'source'},capabilities={'status'})
    monkeypatch.setattr(agent_updates.LauncherUpdates,'desired',Mock(return_value=release))
    code,payload=controls.update_payload({'peers':{'node':'10.0.0.2'},'controller_public_key':'public'}, {'node':'node','machine':'machine','capabilities':['status']})
    assert code==agent_updates.INSTALL and payload['revision']=='published'
    with pytest.raises(ValueError):controls.update_payload({}, {'capabilities':['unpublished-capability']})


@pytest.fixture
def control_server(monkeypatch):
    from http.server import ThreadingHTTPServer
    import task_dashboard,threading
    handler=task_dashboard.Handler
    monkeypatch.setattr(handler,'log_message',lambda *a:None)
    server=ThreadingHTTPServer(('127.0.0.1',0),handler)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    yield server
    server.shutdown();server.server_close();thread.join()


def test_control_http_requires_same_origin_custom_header_and_exact_node(control_server,monkeypatch):
    from http.client import HTTPConnection
    action=Mock(return_value={'accepted':True});monkeypatch.setattr(controls,'request',action)
    host='127.0.0.1:'+str(control_server.server_port)
    def post(origin,extra=None):
        client=HTTPConnection(host)
        headers={'Content-Type':'application/json','Origin':origin,**(extra or {})}
        client.request('POST','/api/tasks/machine/control',json.dumps({'host':'selected.local','action':'pause'}),headers)
        reply=client.getresponse();code=reply.status;reply.read();client.close();return code
    assert post('http://attacker',{'X-JSwarm-Control':'1'})==403
    assert post('http://'+host)==403
    action.assert_not_called()
    assert post('http://'+host,{'X-JSwarm-Control':'1'})==202
    action.assert_called_once_with('selected.local','pause')


def test_update_holds_deployment_lock_until_selected_machine_is_ready(monkeypatch):
    import task_machines,agent_updates
    host={'host':'node','agent_endpoint':'http://10.0.0.2:8790'}
    before={'node':'node','machine':'machine','source_revision':'old'}
    client=Mock();client.call.side_effect=[before,{**before,'source_revision':'published','ready':True}]
    monkeypatch.setattr(controls,'AgentClient',Mock(return_value=client))
    lock=Mock();monkeypatch.setattr(agent_updates,'deployment_lock',Mock(return_value=lock))
    monkeypatch.setattr(controls,'update_payload',Mock(return_value=('fixed installer',{'revision':'published'})))
    command=Mock(return_value=(b'',0,1));monkeypatch.setattr(task_machines,'command',command)
    monkeypatch.setattr(controls,'CACHE',{'node':{'value':{},'action':None}})
    controls.execute(host,{},'update')
    assert controls.CACHE['node']['action']['revision']=='published'
    assert controls.CACHE['node']['action']['status']=='sent'
    assert client.call.call_count==2 and command.call_count==1
    lock.close.assert_called_once()
