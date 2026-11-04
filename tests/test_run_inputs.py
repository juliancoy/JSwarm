import sys
from pathlib import Path
import pytest
from scripts.run_inputs import plan_inputs
sys.path.insert(0,str((Path(__file__).resolve().parents[2] if Path(__file__).resolve().parents[1].name in ('GenomeHarbor', 'JSwarm') else Path(__file__).resolve().parents[1])/'swarm-control/server'))
from run_manager_projection import attach_delegated_runs
from task_progress import aggregate


def jobs(kind='bam-read-qc'):
    return [{'id':str(n),'spec':{'kind':kind,'bytes':100_000_000_000}} for n in range(2)]


def test_auto_streams_two_large_bams_without_cache():
    p=plan_inputs(jobs(),free_cache_bytes=17_000_000_000)
    assert p.mode=='stream' and p.parallelism==2 and p.cache_bytes_required==0
    assert p.buffer_bytes_per_stream==1024*1024


def test_random_access_cannot_be_forced_into_a_stream():
    value=jobs();value[0]['input_access']='random'
    with pytest.raises(ValueError,match='seekable'):plan_inputs(value,mode='stream')
    with pytest.raises(ValueError,match='cache space'):plan_inputs(value)


def test_input_parallelism_respects_memory_and_cpu_admission():
    assert plan_inputs(jobs(),available_memory_mb=2048).parallelism==1
    with pytest.raises(ValueError,match='CPU/RAM'):plan_inputs(jobs(),available_cpus=1)


def test_cached_random_access_reserves_disk_headroom():
    assert plan_inputs(jobs('other'),free_cache_bytes=300_000_000_000).mode=='cache'


def test_delegation_is_grouped_under_original_run_once():
    parent={'id':'parent','name':'DNA','status':'active','delegated_runs':['streams','streams'],'children':[{'id':'one','status':'completed'}]}
    child={'id':'streams','status':'running','children':[{'id':'two','status':'running'},{'id':'three','status':'running'}]}
    grouped,remaining=attach_delegated_runs([parent],[child],aggregate)
    assert not remaining and grouped[0]['progress']['total']==3 and grouped[0]['progress']['completed']==1
    assert len(parent['children'])==1
    parent['status']='completed'
    grouped,_=attach_delegated_runs([parent],[child],aggregate)
    assert grouped[0]['status']=='active' # Native work finished; delegated input remains running.
    child['status']='completed'
    for row in child['children']:row['status']='completed'
    grouped,_=attach_delegated_runs([parent],[child],aggregate)
    assert grouped[0]['status']=='completed'


def test_missing_executor_observation_cannot_complete_parent_run():
    parent={'id':'parent','name':'DNA','status':'completed','delegated_runs':['streams'],
            'delegation_jobs':[{'run':'streams','job':'b','worker':'compai.local'},{'run':'streams','job':'c','worker':'compai.local'}],
            'children':[{'id':'a','status':'completed'}]}
    grouped,_=attach_delegated_runs([parent],[],aggregate)
    assert grouped[0]['status']=='active'
    assert grouped[0]['progress']['total']==3 and grouped[0]['progress']['completed']==1


def test_source_and_receiver_views_do_not_duplicate_delegated_jobs():
    parent={'id':'parent','status':'active','delegated_runs':['streams'],
            'delegation_jobs':[{'run':'streams','job':'b','worker':'compai.local'}],
            'children':[{'id':'a','status':'completed'},{'id':'b','status':'queued'}]}
    child={'id':'streams','status':'running','children':[{'id':'b','status':'running'}]}
    grouped,remaining=attach_delegated_runs([parent],[child],aggregate)
    assert not remaining and grouped[0]['progress']['total']==2
    assert grouped[0]['progress']['completed']==1
