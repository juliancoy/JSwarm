import json
from unittest.mock import MagicMock

from scripts import swarm_runtime as runtime
from scripts import launcher_discovery as discovery


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
    monkeypatch.setattr(discovery.socket, 'getaddrinfo', lambda *args, **kwargs: [(2, 1, 6, '', (resolved, 8790))])
    monkeypatch.setattr(runtime.subprocess, 'check_output', lambda *args, **kwargs: 'true true')
    return factory


def test_mdns_endpoint_survives_changed_enrolled_ip(tmp_path, monkeypatch):
    factory = configure(tmp_path, monkeypatch)
    host, = runtime.discover()
    assert host['eligible']
    assert factory.call_args.args[0]['agent_endpoint'] == 'http://worker.swarm.local:8790'
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


def test_legacy_name_is_only_a_dns_rollout_fallback(monkeypatch):
    calls=[]
    def resolve(name,*args,**kwargs):
        calls.append(name)
        if name.endswith('.swarm.local'):raise discovery.socket.gaierror('not published yet')
        return [(2,1,6,'',('10.42.0.71',8790))]
    monkeypatch.setattr(discovery.socket,'getaddrinfo',resolve)
    endpoint,addresses=discovery.resolve_endpoint('worker.local')
    assert endpoint=='http://worker.local:8790'
    assert addresses==['10.42.0.71']
    assert calls==['worker.swarm.local','worker.local']


def test_dns_sd_accepts_changed_ip_but_rejects_unrelated_target():
    from types import SimpleNamespace
    from unittest.mock import Mock
    instance=discovery.Discovery.__new__(discovery.Discovery)
    import threading
    instance.name='controller.local';instance.peers={'worker.local':'10.42.0.137'}
    instance.lock=threading.Lock();instance.services={}
    info=SimpleNamespace(properties={b'node':b'worker.local',b'protocol':b'lifetech-launcher.v1'},port=8790,
                         server='worker.swarm.local.',parsed_addresses=lambda _:['10.42.0.222'])
    zc=Mock();zc.get_service_info.return_value=info
    instance.update_service(zc,discovery.SERVICE,'service')
    assert instance.endpoint('worker.local')==('worker.swarm.local',8790)
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
