import json
import os
import sys
import pytest
from pathlib import Path
from unittest.mock import Mock, patch

from scripts import swarm_command as command


@pytest.mark.parametrize('route',[None,'auto','webrtc'])
def test_recovery_closes_only_proven_dead_owner(tmp_path,route):
    host={'host':'worker.local','machine':'enrolled-id','agent_endpoint':'http://127.0.0.1:8790'}
    if route:host['agent_route']=route
    live=command.caller_record(host,'a'*32,['secret payload'])
    dead={**live,'id':'b'*32,'start':'impossible-start'}
    for row in (live,dead):command.save_caller(tmp_path/(row['id']+'.json'),row)
    client=Mock();client.call.side_effect=[{'machine':'enrolled-id'},{'closed':True}]
    factory=Mock(return_value=client)
    assert command.reap_orphan_clients(tmp_path,factory)==1
    assert (tmp_path/('a'*32+'.json')).exists()
    assert not (tmp_path/('b'*32+'.json')).exists()
    assert 'secret payload' not in (tmp_path/('a'*32+'.json')).read_text()
    factory.assert_called_once_with(host)
    client.call.assert_any_call('command.close',{'id':'b'*32},timeout=10)


def test_identity_mismatch_retains_recovery_record(tmp_path):
    row=command.caller_record({'host':'worker','machine':'expected','agent_endpoint':'http://worker:8790'},'c'*32,[])
    row['start']='gone';path=tmp_path/('c'*32+'.json');command.save_caller(path,row)
    client=Mock();client.call.return_value={'machine':'different'}
    assert command.reap_orphan_clients(tmp_path,lambda _:client)==0
    assert path.exists() and client.call.call_count==1


@pytest.mark.parametrize('same_machine',[True,False])
def test_recovery_uses_current_mdns_endpoint_only_for_matching_machine(tmp_path,same_machine):
    old={'host':'worker.local','machine':'expected','agent_endpoint':'http://10.42.0.137:8790'}
    row=command.caller_record(old,'e'*32,[]);row['start']='gone'
    command.save_caller(tmp_path/(row['id']+'.json'),row)
    current={**old,'machine':'expected' if same_machine else 'replacement',
             'agent_endpoint':'http://worker-swarm.local:8790','agent_route':'auto'}
    client=Mock();client.call.side_effect=[{'machine':'expected'},{'closed':True}]
    factory=Mock(return_value=client)
    assert command.reap_orphan_clients(tmp_path,factory,current_host=current)==int(same_machine)
    if same_machine:factory.assert_called_once_with(current)
    else:factory.assert_not_called()


def test_foreign_live_namespace_is_preserved():
    row=command.caller_record({'host':'w','machine':'m','agent_endpoint':'http://w'},'d'*32,[])
    row.update(namespace='foreign',container='a'*12)
    with patch.object(command.subprocess,'run',return_value=Mock(returncode=0,stdout='true')):
        assert not command.caller_gone(row)
    with patch.object(command.subprocess,'run',return_value=Mock(returncode=0,stdout='false')):
        assert command.caller_gone(row)


def test_lost_start_ack_still_closes_remote_command_and_retains_journal(tmp_path):
    host={'host':'w','machine':'m','agent_endpoint':'http://w'}
    def call(op,*args,**kwargs):
        if op=='status':return {'machine':'m','capabilities':['node-command-v1']}
        if op=='command.start':raise RuntimeError('lost acknowledgement')
        if op=='command.close':return {'closed':True}
        raise AssertionError(op)
    client=Mock();client.call.side_effect=call
    import pytest
    with patch.object(command,'journal_root',return_value=tmp_path),patch.object(command,'AgentClient',return_value=client):
        with pytest.raises(RuntimeError,match='lost acknowledgement'):command.run(host,['true'])
    assert len(list(tmp_path.glob('*.json')))==1
    assert any(c.args[0]=='command.close' for c in client.call.call_args_list)


def test_recovery_does_not_contact_unrelated_offline_peer(tmp_path):
    row=command.caller_record({'host':'offline','machine':'expected','agent_endpoint':'http://offline'},'e'*32,[])
    row['start']='gone';path=tmp_path/('e'*32+'.json');command.save_caller(path,row)
    factory=Mock(side_effect=AssertionError('Unrelated peer contacted'))
    assert command.reap_orphan_clients(tmp_path,factory,host='healthy')==0
    assert path.exists()
    factory.assert_not_called()
