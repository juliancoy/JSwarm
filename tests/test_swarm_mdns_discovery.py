import json
from unittest.mock import MagicMock

from scripts import swarm_runtime as runtime
from scripts import launcher_discovery as discovery


def test_direct_dns_sd_resolution_does_not_require_host_nss(monkeypatch):
    from types import SimpleNamespace
    info = SimpleNamespace(port=8790, server='worker-swarm.local.',
        properties={b'node': b'worker.local', b'protocol': b'lifetech-launcher.v1'},
        parsed_addresses=lambda _: ['10.42.0.222'])
    zc = MagicMock();zc.get_service_info.return_value = info
    monkeypatch.setattr(discovery, '_RESOLVER', None)
    monkeypatch.setattr(discovery, 'Zeroconf', lambda **_: zc)
    assert discovery.resolve_service('worker.local') == ('worker-swarm.local', ['10.42.0.222'])
    zc.close.assert_not_called()  # Keep one DNS-SD cache for the node lifetime.
    info.server = 'unrelated.local.'
    import pytest
    with pytest.raises(OSError):discovery.resolve_service('worker.local')


def test_service_hostname_is_a_single_mdns_label_even_for_long_node_names():
    assert discovery.service_hostname('compai.local')=='compai-swarm.local'
    node='a'*63+'.local'
    hostname=discovery.service_hostname(node)
    assert hostname.count('.')==1 and len(hostname.split('.')[0])<=63
    assert hostname==discovery.service_hostname(node)


def configure(tmp_path, monkeypatch, *, resolved='10.42.0.222', attested='10.42.0.222', accepting=True):
    config = tmp_path / 'peers.json'
    config.write_text(json.dumps({'name': 'controller.local', 'peers': {'worker.local': '10.42.0.137'}}))
    monkeypatch.setattr(runtime, 'CONFIG', config)
    status = {'ready': True, 'protocol': 'lifetech-agent.v1', 'machine': 'signed-machine',
              'addresses': [attested], 'worker_policy': {'accepting_jobs': accepting},
              'capabilities': ['node-command-v1', 'shared-resources']}
    client = MagicMock()
    client.call.side_effect = lambda op, **kwargs: status if op == 'status' else {'cpus': 4}
    factory = MagicMock(return_value=client)
    monkeypatch.setattr(runtime, 'AgentClient', factory)
    monkeypatch.setattr(discovery, 'resolve_service', lambda node: ('worker-swarm.local', [resolved]))
    monkeypatch.setattr(runtime.subprocess, 'check_output', lambda *args, **kwargs: 'true true')
    return factory


def test_mdns_endpoint_survives_changed_enrolled_ip(tmp_path, monkeypatch):
    factory = configure(tmp_path, monkeypatch)
    host, = runtime.discover()
    assert host['eligible']
    assert factory.call_args.args[0]['agent_endpoint'] == 'http://10.42.0.222:8790'
    assert host['lan_address'] == '10.42.0.222'


def test_mdns_resolution_must_match_signed_interface(tmp_path, monkeypatch):
    configure(tmp_path, monkeypatch, attested='10.42.0.99')
    host, = runtime.discover()
    assert not host['eligible']
    assert 'attested physical interface' in host['reason']


def test_mdns_does_not_bypass_node_pause(tmp_path, monkeypatch):
    configure(tmp_path, monkeypatch, accepting=False)
    host, = runtime.discover()
    assert not host['eligible']
    assert 'declining jobs' in host['reason']


def test_endpoint_uses_dns_sd_address_without_container_nss(monkeypatch):
    def unavailable(*args, **kwargs):
        raise AssertionError('Host NSS must never be called')
    monkeypatch.setattr(discovery.socket, 'getaddrinfo', unavailable)
    resolver=MagicMock(return_value=('worker-swarm.local', ['10.42.0.222']))
    monkeypatch.setattr(discovery, 'resolve_service', resolver)
    assert discovery.resolve_endpoint('worker.local') == ('http://10.42.0.222:8790', ['10.42.0.222'])
    resolver.assert_called_once_with('worker.local')


def test_endpoint_rejects_missing_or_invalid_dns_sd_service(monkeypatch):
    import pytest
    def unavailable(*args, **kwargs):
        raise AssertionError('Host NSS must never be called')
    monkeypatch.setattr(discovery.socket, 'getaddrinfo', unavailable)
    monkeypatch.setattr(discovery, 'resolve_service', MagicMock(side_effect=OSError('invalid advertisement')))
    with pytest.raises(OSError, match='No mDNS endpoint'):
        discovery.resolve_endpoint('worker.local')


def test_dns_sd_accepts_changed_ip_but_rejects_unrelated_target():
    from types import SimpleNamespace
    from unittest.mock import Mock
    instance=discovery.Discovery.__new__(discovery.Discovery)
    import threading
    instance.name='controller.local';instance.peers={'worker.local':'10.42.0.137'}
    instance.lock=threading.Lock();instance.services={}
    info=SimpleNamespace(properties={b'node':b'worker.local',b'protocol':b'lifetech-launcher.v1'},port=8790,
                         server='worker-swarm.local.',parsed_addresses=lambda _:['10.42.0.222'])
    zc=Mock();zc.get_service_info.return_value=info
    instance.update_service(zc,discovery.SERVICE,'service')
    assert instance.endpoint('worker.local')==('worker-swarm.local',8790)
    info.server='attacker.local.'
    instance.update_service(zc,discovery.SERVICE,'service')
    assert instance.endpoint('worker.local') is None


def test_listener_addresses_ignore_bridges_and_saved_subnets():
    from scripts.launcher_registry import listener_addresses
    def interface(name,ip,state='UP',kind=None):
        return {'ifname':name,'operstate':state,'linkinfo':{'info_kind':kind} if kind else {},
                'addr_info':[{'family':'inet','scope':'global','local':ip}]}
    rows=[interface('br-docker','192.168.0.1'),interface('docker0','172.17.0.1'),
          interface('eth9','10.42.0.1','DOWN'),interface('enp7s0','192.168.5.8'),
          interface('wlp6s0','172.20.20.20'),interface('ethvirtual','10.1.0.1',kind='veth')]
    assert listener_addresses(rows)==['192.168.5.8','172.20.20.20']


def test_gateway_handles_own_node_without_lan_resolution(tmp_path):
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from scripts.launcher_peers import JobGateway
    config=tmp_path/'peers.json'
    config.write_text(json.dumps({'name':'worker.local','peers':{'worker.local':'10.42.0.137'}}))
    response=object()
    agent=SimpleNamespace(config=config,handle=AsyncMock(return_value=response))
    gateway=JobGateway.__new__(JobGateway);gateway.agent=agent
    request=SimpleNamespace(headers={'X-Lifetech-Peer':'worker.local'},read=AsyncMock(return_value=b'encrypted'))
    assert asyncio.run(gateway.request(request)) is response
    agent.handle.assert_awaited_once_with(request)


def test_signed_subprocess_preserves_gateway_route_and_journal():
    from scripts.swarm_command import caller_record
    host={'host':'worker.local','local':False,'machine':'signed-machine',
          'agent_endpoint':'http://worker-swarm.local:8790','agent_route':'auto'}
    command=runtime.shell_command(host,['true'])
    assert command[command.index('--route')+1]=='auto'
    assert command[-2:]==['--','true']
    assert caller_record(host,'a'*32,['true'])['agent_route']=='auto'


def test_numeric_agent_connection_never_calls_host_resolver(monkeypatch):
    import sys
    from pathlib import Path
    sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'swarm-control/server'))
    import agent_client
    def forbidden(*args,**kwargs):raise AssertionError('Host resolver called')
    monkeypatch.setattr(agent_client.socket,'getaddrinfo',forbidden)
    sock=MagicMock();monkeypatch.setattr(agent_client.socket,'socket',lambda *args:sock)
    assert agent_client.multipath_connection(('10.42.0.222',8790)) is sock
    sock.connect.assert_called_once_with(('10.42.0.222',8790))


def test_named_agent_connection_resolves_dns_sd_without_nss(tmp_path,monkeypatch):
    import sys
    from pathlib import Path
    sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'swarm-control/server'))
    import agent_client
    config=tmp_path/'peers.json'
    config.write_text(json.dumps({'name':'controller.local','peers':{'worker.local':'old-address'},'key':'ab'*32}))
    monkeypatch.setattr(agent_client,'CONFIG',config)
    def forbidden(*args,**kwargs):raise AssertionError('Host NSS must not be called')
    monkeypatch.setattr(agent_client.socket,'getaddrinfo',forbidden)
    resolver=MagicMock(return_value=('worker-swarm.local',['10.42.0.222']))
    monkeypatch.setattr(discovery,'resolve_service',resolver)
    monkeypatch.setattr(discovery,'network_signature',lambda:(('eth0','10.42.0.1'),))
    sock=MagicMock();monkeypatch.setattr(agent_client.socket,'socket',lambda *args:sock)
    client=agent_client.AgentClient({'host':'worker.local','agent_endpoint':'http://worker-swarm.local:8790'})
    assert client.connection._create_connection(('worker-swarm.local',8790)) is sock
    resolver.assert_called_once_with('worker.local')
    sock.connect.assert_called_once_with(('10.42.0.222',8790))


def test_dns_sd_rebinds_after_boot_network_addresses_change(monkeypatch):
    from types import SimpleNamespace
    info=SimpleNamespace(port=8790,server='worker-swarm.local.',
        properties={b'node':b'worker.local',b'protocol':b'lifetech-launcher.v1'},
        parsed_addresses=lambda _:['10.42.0.222'])
    before=MagicMock();before.get_service_info.return_value=None
    after=MagicMock();after.get_service_info.return_value=info
    monkeypatch.setattr(discovery,'_RESOLVER',None)
    network=[()]
    monkeypatch.setattr(discovery,'network_signature',lambda:network[0])
    factory=MagicMock(side_effect=[before,after]);monkeypatch.setattr(discovery,'Zeroconf',factory)
    import pytest
    with pytest.raises(OSError):discovery.resolve_service('worker.local')
    network[0]=(('eth0','10.42.0.71'),)
    assert discovery.resolve_service('worker.local')[1]==['10.42.0.222']
    before.close.assert_called_once()
    discovery.resolve_service('worker.local')
    assert factory.call_count==2


def test_numeric_discovered_endpoint_fails_over_with_mptcp_without_pinning_first_address(tmp_path,monkeypatch):
    import sys
    from pathlib import Path
    sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'swarm-control/server'))
    import agent_client
    config=tmp_path/'peers.json'
    config.write_text(json.dumps({'name':'controller.local','peers':{'worker.local':'old-address'},'key':'ab'*32}))
    monkeypatch.setattr(agent_client,'CONFIG',config)
    addresses=['10.104.105.132','10.42.0.166']
    monkeypatch.setattr(discovery,'resolve_service',lambda name:('worker-swarm.local',addresses))
    connected=object();connect=MagicMock(side_effect=[TimeoutError('unroutable Wi-Fi'),connected])
    monkeypatch.setattr(agent_client,'multipath_connection',connect)
    client=agent_client.AgentClient({'host':'worker.local','agent_endpoint':'http://10.104.105.132:8790'})
    assert client.connection._create_connection(('10.104.105.132',8790),timeout=15) is connected
    assert [call.args[0] for call in connect.call_args_list]==[(ip,8790) for ip in addresses]
    assert [call.args[1] for call in connect.call_args_list]==[2,2]


def test_signed_command_bootstrap_can_import_discovery_outside_workspace(tmp_path):
    import subprocess,sys
    from pathlib import Path
    script=Path(__file__).resolve().parents[1]/'scripts/swarm_command.py'
    code="import runpy;runpy.run_path("+repr(str(script))+");import scripts.launcher_discovery"
    result=subprocess.run([sys.executable,'-I','-c',code],cwd=tmp_path,capture_output=True,text=True,timeout=15)
    assert result.returncode==0,result.stderr


def test_remote_advertisement_cannot_connect_back_to_controller(tmp_path,monkeypatch):
    import sys
    from pathlib import Path
    sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'swarm-control/server'))
    import agent_client
    config=tmp_path/'peers.json'
    config.write_text(json.dumps({'name':'controller.local','peers':{'worker.local':'unused'},'key':'ab'*32}))
    monkeypatch.setattr(agent_client,'CONFIG',config)
    monkeypatch.setattr(discovery,'resolve_service',lambda name:('worker-swarm.local',['172.20.20.20','10.42.0.49']))
    monkeypatch.setattr(discovery,'network_signature',lambda:(('wlan0','172.20.20.20'),))
    connected=object();connect=MagicMock(return_value=connected)
    monkeypatch.setattr(agent_client,'multipath_connection',connect)
    client=agent_client.AgentClient({'host':'worker.local','agent_endpoint':'http://172.20.20.20:8790'})
    assert client.connection._create_connection(('172.20.20.20',8790),timeout=15) is connected
    assert [call.args[0] for call in connect.call_args_list]==[('10.42.0.49',8790)]
