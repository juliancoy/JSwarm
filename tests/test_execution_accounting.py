"""Host/Docker validation of worker-owned terminal resource receipts."""
import datetime
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid

import pytest
from scripts import launcher_streams as streams
from scripts import launcher_agent as agent


@pytest.mark.parametrize('exit_code',[0,7])
def test_real_worker_accounts_children_before_exit_and_preserves_exit_status(tmp_path,exit_code):
    image='python:3.12-bookworm'
    if subprocess.run(['docker','image','inspect',image],capture_output=True).returncode:pytest.skip('Local Python image unavailable')
    key=uuid.uuid4().hex;name='accounting-test-'+key
    program='import time,sys; data=bytearray(8*1024**2); del data; end=time.process_time()+.25\nwhile time.process_time()<end:sum(i*i for i in range(1000))\nsys.exit('+str(exit_code)+')'
    try:
        result=subprocess.run(['docker','run','--pull=never','--name',name,'--network','none','--read-only','--cpus','1','--memory','64m','--memory-swap','64m','--pids-limit','32','--user',str(os.getuid())+':'+str(os.getgid()),'--mount','type=bind,src='+str(tmp_path)+',dst=/work',image,'python3','-c',streams.EXECUTION_ACCOUNTING_SOURCE,'/work/accounting.json',key,'python3','-c',program],capture_output=True,timeout=120)
        assert result.returncode==exit_code,result.stderr.decode()
        receipt=streams.accounting_fields(json.loads((tmp_path/'accounting.json').read_text()),key)
        assert receipt['exit_code']==exit_code
        assert receipt['cpu_core_seconds']>=.2
        assert receipt['elapsed_seconds']>=.25
        assert receipt['cpu_accounting_coverage']=='final-pre-exit'
        assert 'queue' in receipt['timing_basis']
        assert receipt['memory_accounting_coverage']=='final-pre-exit'
        assert receipt['memory_peak_bytes']>=8*1024**2
        assert receipt['memory_current_bytes']<=receipt['memory_peak_bytes']
        # Restarted owner can recover the metadata after worker exit, before rm.
        packed=subprocess.check_output(['docker','cp',name+':/work/accounting.json','-'])
        assert streams.accounting_tar(packed,key)==receipt
    finally:subprocess.run(['docker','rm','-f',name],capture_output=True)


def test_accounting_write_failure_does_not_change_command_outcome():
    result=subprocess.run([sys.executable,'-c',streams.EXECUTION_ACCOUNTING_SOURCE,'/dev/null/accounting.json','attempt','sh','-c','exit 7'],capture_output=True,text=True)
    assert result.returncode==7
    assert 'SWARM_EXECUTION_ACCOUNTING ' in result.stderr


def test_attempt_binding_rejects_stale_and_nonfinite_counters():
    receipt={'schema':'swarm-execution-accounting.v1','attempt_key':'one','started':'2026-01-01T00:00:00+00:00','finished':'2026-01-01T00:00:02+00:00','elapsed_seconds':2,'cpu_core_seconds':1,'exit_code':0}
    with pytest.raises(ValueError):streams.accounting_fields(receipt,'two')
    with pytest.raises(ValueError):streams.accounting_fields({**receipt,'cpu_core_seconds':float('nan')},'one')


def test_journal_retains_partial_cpu_on_forced_exit_and_survives_restart(tmp_path,monkeypatch):
    key=uuid.uuid4().hex;cid='a'*64
    item={'Id':cid,'Name':'/genomics-'+key,'Image':'sha256:'+'b'*64,'Config':{'Labels':{'org.lifetech.worker':'true','org.lifetech.job':key}},'HostConfig':{'NanoCpus':1000000000,'Memory':64*1024**2},'State':{'Status':'running','Running':True,'StartedAt':'2026-01-01T00:00:00Z','FinishedAt':''}}
    monkeypatch.setitem(agent._WORKER_CPU_CACHE,cid,{'total':2000000000,'time':time.monotonic()})
    monkeypatch.setitem(agent._WORKER_RAM_CACHE,cid,{'raw_bytes':123456,'raw_peak_bytes':234567,'sampled_at':time.time()})
    journal=agent.DockerWorkerJournal(tmp_path/'workers.sqlite');journal.record(item)
    monkeypatch.delitem(agent._WORKER_RAM_CACHE,cid)
    monkeypatch.delitem(agent._WORKER_CPU_CACHE,cid)
    item['State'].update(Status='exited',Running=False,ExitCode=137,OOMKilled=True,FinishedAt='2026-01-01T00:00:05Z')
    value=journal.record(item)
    assert value['cpu_core_seconds']==2
    assert value['cpu_accounting_coverage']=='sampled-partial'
    assert value['elapsed_seconds']==5
    assert value['memory_peak_bytes']==234567
    assert value['memory_accounting_coverage']=='sampled-partial'
    assert agent.DockerWorkerJournal(tmp_path/'workers.sqlite').get(key)['memory_current_bytes']==123456
    assert agent.DockerWorkerJournal(tmp_path/'workers.sqlite').get(key)['exit_code']==137


@pytest.mark.parametrize('counter',[-1,True,float('nan'),2**63])
def test_memory_receipt_rejects_invalid_counters(counter):
    receipt={'schema':'swarm-execution-accounting.v1','attempt_key':'one','started':'2026-01-01T00:00:00+00:00','finished':'2026-01-01T00:00:02+00:00','elapsed_seconds':2,'exit_code':0,'memory_peak_bytes':counter}
    with pytest.raises(ValueError):streams.accounting_fields(receipt,'one')


def test_cleanup_retains_cpu_and_ram_independently():
    saved={'cpu_core_seconds':2,'cpu_accounting_coverage':'final-pre-exit','memory_peak_bytes':300,'memory_accounting_coverage':'final-pre-exit'}
    merged=streams.merge_execution_accounting(saved,{'cpu_core_seconds':1,'cpu_accounting_coverage':'sampled-partial','memory_accounting_coverage':'unavailable'})
    assert merged==saved


def test_journal_retains_terminal_ram_after_receipt_cleanup(tmp_path):
    key=uuid.uuid4().hex;cid='c'*64
    item={'Id':cid,'Name':'/genomics-'+key,'Image':'sha256:'+'d'*64,'Config':{'Labels':{'org.lifetech.worker':'true','org.lifetech.job':key}},'HostConfig':{},'State':{'Status':'exited','Running':False,'ExitCode':0,'StartedAt':'2026-01-01T00:00:00Z','FinishedAt':'2026-01-01T00:00:05Z'}}
    journal=agent.DockerWorkerJournal(tmp_path/'workers.sqlite',accounting_reader=lambda _: {'cpu_accounting_coverage':'unavailable','memory_peak_bytes':999,'memory_current_bytes':111,'memory_accounting_coverage':'final-pre-exit'})
    assert journal.record(item)['memory_peak_bytes']==999
    restarted=agent.DockerWorkerJournal(tmp_path/'workers.sqlite',accounting_reader=lambda _: {'cpu_accounting_coverage':'unavailable','memory_accounting_coverage':'unavailable'})
    value=restarted.record(item)
    assert value['memory_peak_bytes']==999
    assert value['memory_accounting_coverage']=='final-pre-exit'
