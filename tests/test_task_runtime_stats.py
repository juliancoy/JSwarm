import unittest
from scripts.task_runtime_stats import measured


class RuntimeStatsTests(unittest.TestCase):
    def test_rates_use_deltas_and_phase_only_eta(self):
        row={'phase_key':'sha256','attempt_started':100,'unit':'bytes','completed_units':50,'total_units':100,'cpu_seconds':10,'current_phase':'Checksum'}
        old={**row,'sampled_at':110,'completed_units':30,'cpu_seconds':5}
        result=measured(row,old,120)
        self.assertEqual(result['units_per_second'],2)
        self.assertEqual(result['execution_eta_seconds'],25)
        self.assertEqual(result['cpu_percent'],50)
        self.assertEqual(result['elapsed_seconds'],20)
        self.assertEqual(result['eta_scope'],'Checksum')

    def test_unknown_denominator_and_stalled_work_have_no_eta(self):
        for count in (10,20):
            row={'phase_key':'map','unit':'shards','completed_units':count}
            result=measured(row,{**row,'sampled_at':100,'completed_units':10},120)
            self.assertNotIn('execution_eta_seconds',result)

    def test_phase_changes_restarts_resets_and_stale_samples_do_not_generate_rates(self):
        row={'phase_key':'copy','unit':'bytes','completed_units':50,'total_units':100,'attempt_started':100}
        for changes in ({'phase_key':'verify'},{'attempt_started':110},{'completed_units':70},{'sampled_at':1},{'total_units':200}):
            old={**row,'sampled_at':110,'completed_units':20,**changes}
            self.assertNotIn('units_per_second',measured(row,old,120))


def test_source_verification_progress_is_distinct_from_finished_mapping(tmp_path):
    import json
    from scripts.task_runtime_stats import Sampler
    folder=tmp_path/'recall/venter';(folder/'full').mkdir(parents=True)
    (folder/'generation.json').write_text(json.dumps({'generation':'g','work':'/work','started':'2026-11-02T00:00:00+00:00'}))
    (folder/'full/state.json').write_text(json.dumps({'generation':'g','jobs':{'a':{'phase':'complete'}}}))
    sampler=object.__new__(Sampler);sampler.wgs=tmp_path;sampler.state=tmp_path/"state"
    sampler.remote=lambda _: {'available_shards':1,'source_replay':{'phase':'indexed_content_replay','verified_shards':0,'checkpoint_shards':0,'decoded_bytes':123,'decoded_bytes_per_second':12}}
    results=[];sampler.publish=lambda task,child,current:results.append(current)
    sampler.venter()
    assert results[0]['completed_units']==0 and results[0]['mapping_shards_completed']==1
    assert results[0]['unit']=='source shards' and 'Verifying original' in results[0]['current_phase']


def test_replay_has_known_stage_total_and_tail_does_not_reuse_stale_phase(tmp_path):
    import json
    from scripts.task_runtime_stats import Sampler
    root=tmp_path/'recall/venter';(root/'full').mkdir(parents=True)
    (root/'generation.json').write_text(json.dumps({'generation':'g','work':'/work'}))
    (root/'full/state.json').write_text(json.dumps({'generation':'g','jobs':{'a':{'phase':'complete'}}}))
    sampler=object.__new__(Sampler);sampler.wgs=tmp_path;sampler.state=tmp_path/"state";results=[];sampler.publish=lambda task,child,current:results.append(current)
    remote={'available_shards':2,'latest_shard_at':90,'source_archive_bytes':1000000000,'source_replay':{'phase':'indexed_content_replay','verified_shards':1,'updated':100}}
    sampler.remote=lambda _:remote
    sampler.venter();assert results[-1]['completed_units']==1 and results[-1]['total_units']==2
    assert 'Verifying original' in results[-1]['current_phase']
    remote['latest_shard_at']=110
    sampler.venter();row=results[-1]
    assert row['unit']=='available shards' and row['total_units']==2
    assert 'Parsing remaining' in row['current_phase']
    assert 'end-of-file' in row['eta_unavailable_reason'] and '1.00 GB' in row['runtime_detail']


def test_live_source_machine_jobs_require_running_container(tmp_path):
    import json
    from scripts.task_runtime_stats import Sampler
    root=tmp_path/'recall/venter';(root/'full').mkdir(parents=True)
    (root/'generation.json').write_text(json.dumps({'generation':'g','work':'/work'}))
    (root/'full/state.json').write_text(json.dumps({'generation':'g'}))
    (root/'stream.json').write_text(json.dumps({'host':'latitude','container':'parser','phase':'running'}))
    folder=root/'generations/g/full';folder.mkdir(parents=True)
    for name,container in [('SRR-live','mapper'),('SRR-old','exited')]:
        (folder/(name+'.json')).write_text(json.dumps({'host':'latitude','container':container,'cpus':3,'memory_mb':12000}))
    sampler=object.__new__(Sampler);sampler.wgs=tmp_path;sampler.state=tmp_path/'state'
    sampler.remote=lambda _: {'running_containers':['parser','mapper'],'available_shards':42}
    sampler.publish=lambda *args:None
    sampler.venter()
    task=json.loads((sampler.state/'tasks/venter-live-source-worker.json').read_text())
    assert [c['id'] for c in task['children']]==['source-parser','SRR-live']
    assert all(c['worker']=='latitude' for c in task['children'])
