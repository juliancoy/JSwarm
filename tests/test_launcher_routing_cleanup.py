from unittest.mock import MagicMock,patch
import pytest
from scripts.launcher_registry import configure_launcher_routing


def test_rootless_engine_never_creates_host_routing_helpers():
    client=MagicMock();client.info.return_value={'SecurityOptions':['name=rootless']}
    with patch('docker.from_env',return_value=client):
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
