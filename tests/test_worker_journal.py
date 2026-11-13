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


def test_gpu_allocation_identity_survives_journal_restart_without_claiming_kernel_use(tmp_path):
    journal=DockerWorkerJournal(tmp_path/'workers.sqlite');item=container()
    item['Config']['Labels'].update({'org.lifetech.accelerator':'GPU-one','org.lifetech.sink':'gpu'})
    journal.record(item)
    row=DockerWorkerJournal(journal.path).get(JOB)
    assert row['gpu_allocation']=='GPU-one' and row['sink']=='gpu' and not row.get('gpu_used')


def test_journal_rejects_reused_identity_and_unowned_containers(tmp_path):
    journal=DockerWorkerJournal(tmp_path/'workers.sqlite');journal.record(container())
    with pytest.raises(ValueError,match='reused'):journal.record(container(ident='e'*64))
    other=container();other['Config']['Labels']['org.lifetech.worker']='false'
    with pytest.raises(ValueError,match='ownership'):journal.record(other)
    assert journal.get(JOB)['container_id']=='c'*64


def test_status_polls_do_not_commit_unchanged_workers_but_exits_are_durable(tmp_path,monkeypatch):
    import sqlite3
    journal=DockerWorkerJournal(tmp_path/'workers.sqlite')
    clock=iter((100.,101.,102.,103.));monkeypatch.setattr(module.time,'time',lambda:next(clock))
    journal.record(container())
    with sqlite3.connect(journal.path) as observer:
        version=observer.execute('PRAGMA data_version').fetchone()[0]
        assert journal.record(container())['observed_at']==101.
        assert journal.record(container())['observed_at']==102.
        assert observer.execute('PRAGMA data_version').fetchone()[0]==version
        journal.record(container(False))
        assert observer.execute('PRAGMA data_version').fetchone()[0]!=version
    recovered=DockerWorkerJournal(journal.path).get(JOB)
    assert recovered['status']=='exited' and recovered['observed_at']==103.


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


@pytest.mark.parametrize('raw',[b'',b'{"status":',b'null',b'[]',b'{}',b'\xff'])
def test_corrupt_legacy_state_does_not_block_other_jobs(tmp_path,raw):
    broken=tmp_path/'broken';broken.mkdir();(broken/'state.json').write_bytes(raw)
    (broken/'source').mkdir();(broken/'source'/'keep').write_text('evidence')
    running=tmp_path/'running';running.mkdir();(running/'state.json').write_text('{"status":"running"}')
    completed=tmp_path/'completed';completed.mkdir();(completed/'state.json').write_text('{"status":"completed"}')
    remove=Mock()
    errors=module.recover_job_states(tmp_path,remove)
    assert len(errors)==1 and errors[0]['job']=='broken'
    assert next(broken.glob('state.corrupt-*.json')).read_bytes()==raw
    assert json.loads((broken/'state.json').read_text())['status']=='unknown'
    assert (broken/'source'/'keep').read_text()=='evidence'
    assert not (broken/'STOP').exists()
    assert json.loads((running/'state.json').read_text())['status']=='interrupted'
    assert (running/'STOP').exists()
    assert json.loads((completed/'state.json').read_text())['status']=='completed'
    remove.assert_called_once_with('running')
    module.recover_job_states(tmp_path,remove)
    assert len(list(broken.glob('state.corrupt-*.json')))==1
    remove.assert_called_once_with('running')


def test_failed_checkpoint_write_preserves_previous_json(tmp_path,monkeypatch):
    path=tmp_path/'state.json';path.write_text('{"status":"running"}')
    def full(*args):raise OSError('disk full')
    monkeypatch.setattr(module.os,'fsync',full)
    with pytest.raises(OSError,match='disk full'):module.write_job_state(path,{'status':'completed'})
    assert json.loads(path.read_text())=={'status':'running'}
    assert list(tmp_path.iterdir())==[path]


def test_state_recovery_skips_symlinks(tmp_path):
    outside=tmp_path/'outside.json';outside.write_text('broken')
    folder=tmp_path/'job';folder.mkdir();(folder/'state.json').symlink_to(outside)
    remove=Mock();assert module.recover_job_states(tmp_path,remove)==[]
    assert outside.read_text()=='broken';remove.assert_not_called()
