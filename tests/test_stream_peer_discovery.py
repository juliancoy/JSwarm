import sys
from types import SimpleNamespace
from unittest.mock import Mock

from scripts import launcher_streams as streams


def test_peer_connect_preserves_scopes_and_tries_other_families(monkeypatch):
    connection=SimpleNamespace();monkeypatch.setattr(streams.http.client,'HTTPSConnection',lambda *a,**kw:connection)
    monkeypatch.setattr(streams,'peer_addresses',lambda *a:['fe80::1%enp7s0','192.0.2.10'])
    attempts=[];success=object()
    def connect(address,timeout,source_address):
        attempts.append((address,timeout))
        if ':' in address[0]:raise OSError('IPv6 route unavailable')
        return success
    monkeypatch.setattr(streams.socket,'create_connection',connect)
    result=streams.peer_https_connection({'address':'enrolled-swarm.local'},9448,None,'enrolled.local')
    assert result._create_connection(('enrolled-swarm.local',9448),60) is success
    assert attempts==[(('fe80::1%enp7s0',9448),5),(('192.0.2.10',9448),5)]


def test_peer_connect_refreshes_discovery_after_all_paths_disappear(monkeypatch):
    connection=SimpleNamespace();monkeypatch.setattr(streams.http.client,'HTTPSConnection',lambda *a,**kw:connection)
    refreshes=[];success=object()
    def addresses(node,refresh):
        refreshes.append(refresh)
        return ['192.0.2.11' if refresh else '192.0.2.10']
    monkeypatch.setattr(streams,'peer_addresses',addresses)
    def connect(address,**kwargs):
        if address[0].endswith('.10'):raise OSError('Old interface disappeared')
        return success
    monkeypatch.setattr(streams.socket,'create_connection',connect)
    result=streams.peer_https_connection({'address':'enrolled.local'},9448,None,'enrolled.local')
    assert result._create_connection(('enrolled.local',9448),60) is success
    assert refreshes==[False,True]


def test_ordinary_hostname_connection_remains_available(monkeypatch):
    connection=SimpleNamespace();monkeypatch.setattr(streams.http.client,'HTTPSConnection',lambda *a,**kw:connection)
    monkeypatch.setattr(streams,'peer_addresses',lambda *a:[])
    connect=Mock();monkeypatch.setattr(streams.socket,'create_connection',connect)
    result=streams.peer_https_connection({'address':'enrolled.local'},9448,None,'enrolled.local')
    result._create_connection(('enrolled.local',9448),60)
    connect.assert_called_once_with(('enrolled.local',9448),timeout=5,source_address=None)


def test_dns_sd_filters_foreign_services_and_unscoped_link_local(monkeypatch):
    info=SimpleNamespace(port=8790,properties={b'node':b'enrolled.local',b'protocol':b'lifetech-launcher.v1'},
                         parsed_scoped_addresses=lambda version:['fe80::1','fe80::1%2','192.0.2.10','192.0.2.10'])
    discovery=SimpleNamespace(get_service_info=lambda *a,**k:info,close=lambda:None)
    monkeypatch.setitem(sys.modules,'zeroconf',SimpleNamespace(Zeroconf=lambda **k:discovery,IPVersion=SimpleNamespace(All='all')))
    monkeypatch.setattr(streams,'_DISCOVERY',None)
    assert streams.peer_addresses('enrolled.local')==['fe80::1%2','192.0.2.10']
    info.properties[b'node']=b'foreign.local'
    assert streams.peer_addresses('enrolled.local')==[]
