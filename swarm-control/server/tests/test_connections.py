import json
import threading
import time
from connections import Connections


def test_disconnect_reconnect_and_paused_worker_are_distinct(tmp_path):
    now=[100.];up=[True]
    def probe(host,address):
        if not up[0]:raise OSError('offline')
        return {'ready':True,'accepting_jobs':False}
    monitor=Connections(probe=probe,clock=lambda:now[0])
    monitor.check('laptop.local','10.0.0.2')
    row=monitor.view()['hosts'][0]
    assert row['status']=='connected' and not row['accepting_jobs']
    up[0]=False;now[0]=101;monitor.check('laptop.local','10.0.0.2')
    assert monitor.view()['hosts'][0]['status']=='disconnected'
    assert monitor.view()['hosts'][0]['last_seen']==100
    up[0]=True;now[0]=102;monitor.check('laptop.local','10.0.0.2')
    assert monitor.view()['hosts'][0]['connected_at']==102
    now[0]=108
    assert monitor.view()['hosts'][0]['status']=='unknown'


def test_auth_failure_is_not_a_connected_machine():
    def probe(*args):raise PermissionError('denied')
    monitor=Connections(probe=probe);monitor.check('peer.local','10.0.0.2')
    assert monitor.view()['hosts'][0]['status']=='authentication_denied'


def test_slow_peer_does_not_block_joining_and_config_changes(tmp_path):
    config=tmp_path/'peers.json';config.write_text(json.dumps({'peers':{'slow.local':'10.0.0.2','fast.local':'10.0.0.3'}}))
    waiting=threading.Event();release=threading.Event();stop=threading.Event()
    def probe(host,address):
        if host=='slow.local':waiting.set();release.wait(3)
        return {'ready':True,'accepting_jobs':True}
    monitor=Connections(config=config,probe=probe);thread=threading.Thread(target=monitor.worker,args=(stop,));thread.start()
    try:
        assert waiting.wait(1)
        deadline=time.monotonic()+1
        while time.monotonic()<deadline:
            if any(row['host']=='fast.local' and row['status']=='connected' for row in monitor.view()['hosts']):break
            time.sleep(.01)
        else:raise AssertionError('Healthy host waited for slow host')
        config.write_text(json.dumps({'peers':{'new.local':'10.0.0.4'}}))
        deadline=time.monotonic()+1
        while time.monotonic()<deadline:
            rows=monitor.view()['hosts']
            if [r['host'] for r in rows]==['new.local'] and rows[0]['status']=='connected':break
            time.sleep(.01)
        else:raise AssertionError('Enrollment change was not detected')
        release.set();time.sleep(.15)
        assert [r['host'] for r in monitor.view()['hosts']]==['new.local']
    finally:release.set();stop.set();thread.join(3)


def test_heartbeat_rejects_wrong_peer_identity():
    from unittest.mock import patch
    with patch('connections.AgentClient') as client:
        client.return_value.call.return_value={'protocol':'lifetech-agent.v1','node':'different.local','machine':'id'}
        monitor=Connections();monitor.check('requested.local','10.0.0.2')
        assert monitor.view()['hosts'][0]['status']=='disconnected'
        client.return_value.close.assert_called_once()


def test_remote_connection_projection_drops_unapproved_fields():
    from unittest.mock import patch
    from status import connections
    with patch('status.read_coordinator',return_value={'hosts':[{'host':'peer','status':'connected','secret':'private','hardware':{'detail':'private'}}],'sampled_at':123,'credentials':'private'}):
        result=connections()
    assert result=={'sampled_at':123,'hosts':[{'host':'peer','status':'connected'}]}
