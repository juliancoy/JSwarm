from collections import deque
import sqlite3
import pytest
from scripts.launcher_agent import JobEventWriter,persist_job_event,persisted_job_events


def test_reused_journal_commits_before_publishing_and_survives_reopen(tmp_path):
    job={'sequence':0,'events':deque()}
    writer=JobEventWriter(tmp_path)
    try:
        for n in range(5):persist_job_event(tmp_path,'job',job,{'kind':'artifact_chunk','data':str(n)},writer=writer)
        assert len(persisted_job_events(tmp_path,'job',0))==5
        seq=persist_job_event(tmp_path,'job',job,{'kind':'suite','status':'passed'},writer=writer,publish=False)
        assert seq==6 and job['sequence']==5
        assert persisted_job_events(tmp_path,'job',5)[0]['event']['status']=='passed'
    finally:writer.close()
    assert persisted_job_events(tmp_path,'job',5)[0]['sequence']==6


def test_failed_commit_never_advances_published_sequence(tmp_path):
    job={'sequence':0,'events':deque()};writer=JobEventWriter(tmp_path)
    try:
        writer.append('job',1,{'kind':'log'})
        with pytest.raises(sqlite3.IntegrityError):persist_job_event(tmp_path,'job',job,{'kind':'log'},writer=writer)
        assert job['sequence']==0 and not job['events']
    finally:writer.close()
