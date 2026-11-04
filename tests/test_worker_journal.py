import json
import subprocess
import threading
from unittest.mock import Mock
import pytest
from scripts import launcher_agent as module
from scripts.launcher_agent import Agent,DockerWorkerJournal

JOB='a'*32
NAME='genomics-'+JOB


def container(running=True,ident='c'*64):
    return {'Id':ident,'Name':'/'+NAME,'Image':'sha256:'+'d'*64,
            'Config':{'Labels':{'org.lifetech.worker':'true','org.lifetech.job':JOB,'org.lifetech.run':'existing-shard-42','org.lifetech.group':'alignment','org.lifetech.sink':'cpu'}},
            'HostConfig':{'NanoCpus':2_000_000_000,'Memory':1024**3},
            'State':{'Status':'running' if running else 'exited','Running':running,'ExitCode':0,'StartedAt':'2026-11-03T00:00:00Z','FinishedAt':'2026-11-03T00:01:00Z'}}


def test_restart_keeps_existing_job_and_container_ids_and_terminal_result(tmp_path):
    path=tmp_path/'workers.sqlite';journal=DockerWorkerJournal(path)
    first=journal.record(container());assert first['exit_code'] is None
    recovered=DockerWorkerJournal(path)
    assert recovered.get(JOB)['container_id']=='c'*64
    recovered.record(container(False))
    terminal=DockerWorkerJournal(path).get(JOB)
    assert terminal['exit_code']==0 and not terminal['running']
    assert terminal['run']=='existing-shard-42'
    assert [row['job'] for row in recovered.records()]==[JOB]
    assert terminal['cpus']==2 and terminal['memory_mb']==1024


def test_journal_rejects_reused_identity_and_unowned_containers(tmp_path):
    journal=DockerWorkerJournal(tmp_path/'workers.sqlite');journal.record(container())
    with pytest.raises(ValueError,match='reused'):journal.record(container(ident='e'*64))
    other=container();other['Config']['Labels']['org.lifetech.worker']='false'
    with pytest.raises(ValueError,match='ownership'):journal.record(other)
    assert journal.get(JOB)['container_id']=='c'*64


def test_startup_reconciles_docker_before_readiness_without_launch_or_stop(tmp_path,monkeypatch):
    agent=Agent.__new__(Agent);agent.worker_journal=DockerWorkerJournal(tmp_path/'workers.sqlite')
    replies=[subprocess.CompletedProcess([],0,'c'*64+'\n',''),subprocess.CompletedProcess([],0,json.dumps([container()]),'')]
    run=Mock(side_effect=replies);monkeypatch.setattr(module.subprocess,'run',run)
    agent.recover_docker_workers()
    assert agent.worker_journal.get(JOB)['container']==NAME
    assert [call.args[0][1] for call in run.call_args_list]==['ps','inspect']


def test_missing_container_retains_completion_but_does_not_invent_success(tmp_path,monkeypatch):
    agent=Agent.__new__(Agent);agent.worker_journal=DockerWorkerJournal(tmp_path/'workers.sqlite')
    monkeypatch.setattr(module.subprocess,'run',lambda *a,**k:subprocess.CompletedProcess([],1,'','Error: No such object: '+NAME))
    args={'job':JOB,'container':NAME}
    assert agent.worker_status(args)=={'found':False,'last_observation':None}
    agent.worker_journal.record(container(False))
    assert agent.worker_status(args)['last_observation']['exit_code']==0


def test_worker_lookup_checks_labels_not_just_name(tmp_path,monkeypatch):
    agent=Agent.__new__(Agent);agent.worker_journal=DockerWorkerJournal(tmp_path/'workers.sqlite')
    row=container();row['Config']['Labels']['org.lifetech.job']='b'*32
    monkeypatch.setattr(module.subprocess,'run',lambda *a,**k:subprocess.CompletedProcess([],0,json.dumps([row]),''))
    with pytest.raises(ValueError,match='ownership'):agent.worker_status({'job':JOB,'container':NAME})


def test_exit_event_keeps_result_after_container_cleanup_race(tmp_path):
    journal=DockerWorkerJournal(tmp_path/'workers.sqlite');journal.record(container())
    event={'Action':'die','time':1793664060,'Actor':{'ID':'c'*64,'Attributes':{'org.lifetech.worker':'true','org.lifetech.job':JOB,'exitCode':'17'}}}
    journal.finish_event(event)
    value=DockerWorkerJournal(journal.path).get(JOB)
    assert value['exit_code']==17 and not value['running']
    assert value['image']=='sha256:'+'d'*64
    event['Actor']['ID']='e'*64
    with pytest.raises(ValueError,match='identity differs'):journal.finish_event(event)
    assert journal.get(JOB)['exit_code']==17


def test_startup_ignores_stopped_duplicate_without_replacing_attempt(tmp_path,monkeypatch):
    agent=Agent.__new__(Agent);agent.worker_journal=DockerWorkerJournal(tmp_path/'workers.sqlite')
    agent.worker_journal.record(container(False))
    stale=container(False,ident='e'*64)
    replies=[subprocess.CompletedProcess([],0,'e'*64+'\n',''),subprocess.CompletedProcess([],0,json.dumps([stale]),'')]
    monkeypatch.setattr(module.subprocess,'run',Mock(side_effect=replies))
    agent.recover_docker_workers()
    assert agent.worker_journal.get(JOB)['container_id']=='c'*64


def test_startup_still_rejects_live_duplicate_attempt(tmp_path,monkeypatch):
    agent=Agent.__new__(Agent);agent.worker_journal=DockerWorkerJournal(tmp_path/'workers.sqlite')
    agent.worker_journal.record(container(False))
    live=container(True,ident='e'*64)
    replies=[subprocess.CompletedProcess([],0,'e'*64+'\n',''),subprocess.CompletedProcess([],0,json.dumps([live]),'')]
    monkeypatch.setattr(module.subprocess,'run',Mock(side_effect=replies))
    with pytest.raises(ValueError,match='reused'):agent.recover_docker_workers()


@pytest.mark.parametrize('group',['stream-work','shared-cache'])
def test_startup_leaves_recreated_stream_and_cache_containers_to_their_own_journals(tmp_path,monkeypatch,group):
    agent=Agent.__new__(Agent);agent.worker_journal=DockerWorkerJournal(tmp_path/'workers.sqlite')
    original=container(False);original['Config']['Labels']['org.lifetech.group']=group
    agent.worker_journal.record(original)
    recreated=container(ident='e'*64);recreated['Config']['Labels']['org.lifetech.group']=group
    replies=[subprocess.CompletedProcess([],0,'e'*64+'\n',''),subprocess.CompletedProcess([],0,json.dumps([recreated]),'')]
    run=Mock(side_effect=replies);monkeypatch.setattr(module.subprocess,'run',run)
    agent.recover_docker_workers()
    assert agent.worker_journal.get(JOB)['container_id']=='c'*64
    assert [call.args[0][1] for call in run.call_args_list]==['ps','inspect']
