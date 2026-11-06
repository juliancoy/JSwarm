import json
from pathlib import Path
import subprocess
import tempfile
import threading
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from scripts.launcher_agent import Agent
from scripts.launcher_commands import CommandService, CommandProxy
from scripts.launcher_streams import Engine, worker_storage_policy
from scripts import swarm_runtime


@pytest.fixture
def storage_agent(tmp_path, monkeypatch):
    monkeypatch.delenv('JSWARM_WORKER_STORAGE', raising=False)
    config=tmp_path/'peers.json';config.write_text('{}')
    agent=Agent.__new__(Agent);agent.config=config;agent.lock=threading.RLock()
    agent.root=tmp_path/'jobs';agent.root.mkdir()
    agent.stream_work=Engine(agent,Mock(),lambda _: {'accepting_jobs':True},start=False)
    agent.commands=Mock()
    yield agent
    agent.stream_work.close()


def test_storage_setting_persists_and_environment_pins_it(storage_agent, monkeypatch):
    agent=storage_agent
    assert agent.dispatch('worker.storage', {})['mode']=='durable'
    agent.dispatch('worker.storage.set', {'mode':'ram-only'})
    assert worker_storage_policy(agent.config)['mode']=='ram-only'
    assert (agent.config.parent/'worker-storage.json').stat().st_mode & 0o777==0o600
    monkeypatch.setenv('JSWARM_WORKER_STORAGE','ram-only')
    with pytest.raises(ValueError,match='pinned'):
        agent.dispatch('worker.storage.set', {'mode':'durable'})
    assert agent.dispatch('worker.storage', {})['source']=='environment'
    monkeypatch.setenv('JSWARM_WORKER_STORAGE','typo')
    with pytest.raises(ValueError,match='Invalid'):
        agent.dispatch('worker.storage', {})


def test_corrupt_storage_configuration_fails_closed(storage_agent):
    (storage_agent.config.parent/'worker-storage.json').write_text('{bad')
    with pytest.raises(ValueError):storage_agent.dispatch('worker.storage', {})


def test_owner_filesystem_probe_accepts_ram_and_rejects_disk_and_symlink(tmp_path):
    service=CommandService()
    try:
        swaps=tmp_path/'swaps';swaps.write_text('Filename Type Size Used Priority\n')
        service.owner_spawn=lambda argv:subprocess.Popen([*argv,str(swaps)],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
        with tempfile.TemporaryDirectory(dir='/dev/shm') as ram:
            assert service.dispatch('command.storage',{'id':'a'*32,'paths':[ram]})['validated']
            swaps.write_text('Filename Type Size Used Priority\n/swap.img file 4096 0 -2\n')
            with pytest.raises(ValueError,match='swap to be disabled'):
                service.dispatch('command.storage',{'id':'a'*32,'paths':[ram]})
            swaps.write_text('Filename Type Size Used Priority\n')
            disk=Path(__file__).resolve()
            if subprocess.check_output(['findmnt','-n','-o','FSTYPE','--target',str(disk)],text=True).strip() in ('tmpfs','ramfs'):
                pytest.skip('Filesystem rejection check needs a disk-backed source')
            link=Path(ram)/'disk';link.symlink_to(disk)
            with pytest.raises(ValueError,match='not RAM-backed'):
                service.dispatch('command.storage',{'id':'a'*32,'paths':[str(link)]})
    finally:service.close()


def test_storage_probe_uses_current_command_runtime():
    proxy=CommandProxy.__new__(CommandProxy);proxy.socket=Path('/current.sock');proxy._call=Mock(return_value={'validated':True})
    args={'id':'a'*32,'paths':['/dev/shm/work']}
    assert proxy.dispatch('command.storage',args)['validated']
    proxy._call.assert_called_once_with('command.storage',args,proxy.socket)


def test_policy_is_checked_once_for_new_commands_but_not_reconnections():
    service=CommandService(spawn=lambda argv:subprocess.Popen(argv,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE));validate=Mock()
    args={'id':'c'*32,'argv':['true']}
    try:
        service.dispatch('command.start',args,validate=validate)
        validate.side_effect=ValueError('Policy changed')
        service.dispatch('command.start',args,validate=validate)
        validate.assert_called_once_with(['true'])
        with pytest.raises(ValueError,match='Policy changed'):
            service.dispatch('command.start',{**args,'id':'d'*32},validate=validate)
    finally:service.close()


def worker_command(*mounts):
    return ['docker','run','--label','org.lifetech.worker=true','--read-only',
            '--memory','128m','--memory-swap','128m','--log-driver','none',*mounts,'image','true']


def test_node_checks_legacy_controller_mounts_and_requires_ram_volumes(storage_agent):
    agent=storage_agent;agent.dispatch('worker.storage.set',{'mode':'ram-only'})
    argv=worker_command('--mount','type=bind,src=/dev/shm/job,dst=/work')
    agent.validate_worker_command(argv)
    assert agent.commands.dispatch.call_args.args[1]['paths']==['/dev/shm/job']
    agent.commands.reset_mock()
    with pytest.raises(ValueError,match='read-only'):
        agent.validate_worker_command([a for a in argv if a!='--read-only'])
    with pytest.raises(ValueError,match='no-swap'):
        agent.validate_worker_command([('256m' if index and argv[index-1]=='--memory-swap' else value) for index,value in enumerate(argv)])
    with pytest.raises(ValueError,match='none log driver'):
        agent.validate_worker_command([('local' if a=='none' else a) for a in argv])
    with pytest.raises(ValueError,match='explicit --mount'):
        agent.validate_worker_command(worker_command('-v','/disk:/work'))
    volume={'Driver':'local','Options':{}}
    agent.client=Mock();agent.client.return_value.__enter__=Mock(return_value=SimpleNamespace(volumes=SimpleNamespace(get=lambda _:SimpleNamespace(attrs=volume))))
    agent.client.return_value.__exit__=Mock(return_value=False)
    with pytest.raises(ValueError,match='tmpfs volumes'):
        agent.validate_worker_command(worker_command('--mount','type=volume,src=data,dst=/dev/shm'))
    volume['Options']={'type':'tmpfs','device':'tmpfs'}
    agent.validate_worker_command(worker_command('--mount','type=volume,src=data,dst=/dev/shm'))


def test_generic_worker_rejects_disk_before_reserving_capacity(tmp_path):
    operations=[]
    def call(op,args=None,**kwargs):
        operations.append(op)
        if op=='status':return {'machine':'id','storage_policy':{'mode':'ram-only'}}
        if op=='worker.storage.validate':raise ValueError('Worker mount is not RAM-backed')
        raise AssertionError(op)
    with patch.object(swarm_runtime,'AgentClient') as client:
        client.return_value.call.side_effect=call
        with pytest.raises(ValueError,match='not RAM-backed'):
            swarm_runtime.run_job({'host':'node','local':False,'machine':'id'},'image','true',
                [('/disk/job','/work',False)],'/disk/job',1,128,tmp_path/'report.json',group='test')
    assert operations==['status','worker.storage.validate']
