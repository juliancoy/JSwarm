from unittest.mock import MagicMock,patch
import pytest
from scripts.launcher_registry import configure_launcher_routing


def test_rootless_engine_never_creates_host_routing_helpers():
    client=MagicMock();client.info.return_value={'SecurityOptions':['name=rootless']}
    with patch('docker.from_env',return_value=client), patch('os.access',return_value=False):
        assert configure_launcher_routing({})['status']=='unavailable'
    client.containers.create.assert_not_called()
    client.close.assert_called_once()


def test_failed_helper_start_still_removes_created_container_and_closes_client():
    client=MagicMock();client.info.return_value={'SecurityOptions':[]}
    helper=client.containers.create.return_value
    helper.start.side_effect=RuntimeError('cannot start')
    with patch('docker.from_env',return_value=client):
        with pytest.raises(RuntimeError,match='cannot start'):configure_launcher_routing({})
    helper.remove.assert_called_once_with(force=True)
    client.close.assert_called_once()


def test_web_port_forwarding_is_atomic_scoped_and_idempotent():
    import json
    import subprocess
    from scripts.launcher_registry import reconcile_node_web_ports
    calls=[];tables={}
    def nft(command,**kwargs):
        args=command[1:];calls.append((args,kwargs))
        if args==['-j','list','tables']:
            result={'nftables':[{'table':{'family':family,'name':'jswarm_node_web'}} for family in tables]}
        elif args[:3]==['-j','list','table']:
            result={'nftables':[{'table':{'comment':tables[args[3]]}}]}
        else:
            batch=kwargs['input']
            assert 'flush' not in batch and 'table ip nat' not in batch
            assert 'tcp dport 80' in batch and '127.0.0.1' in batch and '::1' in batch
            assert 'priority -100;' in batch and 'priority dstnat' not in batch
            assert '10.42.0.49:8790' in batch and 'redirect to :8791' in batch
            marker=batch.split('comment "',1)[1].split('"',1)[0]
            tables.update(ip=marker,ip6=marker);result={}
        return subprocess.CompletedProcess(command,0,json.dumps(result),'')
    with patch('subprocess.run',side_effect=nft):
        assert reconcile_node_web_ports('10.42.0.49')['port']==80
        reconcile_node_web_ports('10.42.0.49')
    assert sum(args==['-f','-'] for args,_ in calls)==1


def test_web_port_forwarding_does_not_replace_foreign_tables():
    import json
    import subprocess
    from scripts.launcher_registry import reconcile_node_web_ports
    outputs=[{'nftables':[{'table':{'family':'ip','name':'jswarm_node_web'}}]},
             {'nftables':[{'table':{'comment':'another-application'}}]}]
    with patch('subprocess.run',side_effect=[subprocess.CompletedProcess([],0,json.dumps(row),'') for row in outputs]) as run:
        with pytest.raises(RuntimeError,match='owned by another'):reconcile_node_web_ports('10.42.0.49')
    assert run.call_count==2


def test_lost_create_response_removes_helper_by_preselected_name():
    client=MagicMock();client.info.return_value={'SecurityOptions':[]}
    client.containers.create.side_effect=TimeoutError('lost create response')
    with patch('docker.from_env',return_value=client):
        with pytest.raises(TimeoutError,match='lost create'):configure_launcher_routing({})
    name=client.containers.create.call_args.kwargs['name']
    client.api.remove_container.assert_called_once_with(name,force=True)
    client.close.assert_called_once()


def test_failed_routing_cleanup_prevents_more_creates(monkeypatch):
    from scripts import launcher_registry as registry
    monkeypatch.setattr(registry,'_routing_pending',None)
    client=MagicMock();client.info.return_value={'SecurityOptions':[]}
    client.containers.create.side_effect=TimeoutError('lost create response')
    client.api.remove_container.side_effect=TimeoutError('Docker unavailable')
    with patch('docker.from_env',return_value=client):
        for _ in range(3):
            with pytest.raises(TimeoutError):configure_launcher_routing({})
    assert client.containers.create.call_count==1
    assert registry._routing_pending==client.containers.create.call_args.kwargs['name']


def test_web_forwarding_covers_physical_addresses_and_removes_stale_address():
    import json
    import subprocess
    from scripts.launcher_registry import reconcile_node_web_ports
    batches=[]
    def nft(command, **kwargs):
        if command[1:]==['-j','list','tables']:
            result={'nftables':[{'table':{'family':'ip','name':'jswarm_node_web'}}]}
        elif command[1:4]==['-j','list','table']:
            result={'nftables':[{'table':{'comment':'jswarm-node-ui:old'}}]}
        else:
            batches.append(kwargs['input']);result={}
        return subprocess.CompletedProcess(command,0,json.dumps(result),'')
    with patch('subprocess.run',side_effect=nft):
        reconcile_node_web_ports('10.42.0.137',['10.42.0.222','10.42.0.71'])
    assert len(batches)==1
    assert 'delete table ip jswarm_node_web' in batches[0]
    for address in ('10.42.0.222','10.42.0.71'):
        assert batches[0].count(f'dnat to {address}:8790')==2
    assert '10.42.0.137' not in batches[0]


def test_host_routing_uses_accessible_host_engine_without_changing_job_engine():
    from scripts import launcher_registry as registry
    rootless=MagicMock();rootless.info.return_value={'SecurityOptions':['name=rootless']}
    host=MagicMock();host.info.return_value={'SecurityOptions':[]}
    helper=host.containers.create.return_value
    helper.wait.return_value={'StatusCode':0};helper.logs.return_value=b'{"status":"configured"}'
    with patch('docker.from_env',return_value=rootless), patch('docker.DockerClient',return_value=host) as connect, patch.object(registry.Path,'exists',return_value=False), patch('os.access',return_value=True):
        assert configure_launcher_routing({})['status']=='configured'
    connect.assert_called_once_with(base_url='unix:///var/run/docker.sock',timeout=20)
    rootless.containers.create.assert_not_called()
    assert host.containers.create.call_args.kwargs['cap_add']==['NET_ADMIN']
    rootless.close.assert_called_once();host.close.assert_called_once()
