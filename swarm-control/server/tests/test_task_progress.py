import unittest
from task_progress import peer_task, application_test_task

class TaskProgressTests(unittest.TestCase):
    def test_focused_preparation_never_claims_p2p_is_selected(self):
        task=application_test_task({'id':'run','status':'preparing','selection':{'paths':['tests/one.py'],'suites':[]},
            'hosts':[{'host':'node','session':{'verified':True},'jobs':{'p2p':{'name':'Cross-device WebRTC P2P','status':'queued'}}}]})
        self.assertEqual(task['children'][0]['id'],'worker-preparation')
        self.assertEqual(task['children'][0]['status'],'preparing')
        self.assertIn('1/1 workers verified',task['children'][0]['detail'])
        self.assertNotIn('p2p',str(task))

    def test_missing_inputs_propagate_without_becoming_queued(self):
        from task_registry import task_view
        task=task_view({'status':'partial','children':[{'id':'prepare','status':'queued','children':[
            {'status':'completed'},{'status':'partial'},{'status':'waiting_inputs','detail':'Full HuRef confidence evidence is missing'}]}]})
        self.assertEqual(task['status'],'partial')
        self.assertEqual(task['children'][0]['status'],'waiting_inputs')
        self.assertEqual(task['input_blockers'],['Full HuRef confidence evidence is missing'])
        partial=task_view({'status':'partial','children':[{'status':'completed'}]})
        self.assertEqual(partial['status'],'partial')

    def test_stale_worker_cannot_claim_running_or_completion(self):
        manifest={'id':'run','jobs':[{'id':'a','preferred_worker':'peer','spec':{'kind':'bam-read-qc','path':'reads.bam','scope':'QC'}}]}
        state={'node':'peer','time':1,'current_job':'a','completed':['a']}
        task=peer_task(manifest,state,{},now=100)
        self.assertTrue(task['stale']);self.assertEqual(task['progress']['percent'],0)
        self.assertEqual(task['children'][0]['status'],'unknown')
        self.assertIsNone(task['children'][0]['percent'])
        task=peer_task(manifest,state,{'a':{'node':'peer'}},now=100)
        self.assertEqual(task['progress']['percent'],100)
    def test_duplicate_test_attempts_count_once(self):
        task=application_test_task({'id':'run','task_catalog':['unit','browser'],'hosts':[
            {'host':'a','jobs':{'unit':{'status':'failed'}}},
            {'host':'b','jobs':{'unit':{'status':'passed'},'browser':{'status':'running'}}}]})
        self.assertEqual(task['progress']['total'],2)
        self.assertEqual(task['progress']['percent'],50)
        self.assertEqual(task['children'][0]['worker'],'b')
    def test_conflicts_not_counted_as_success(self):
        task=peer_task({'id':'r','jobs':[{'id':'a','preferred_worker':'p','spec':{'kind':'fasta-summary','path':'a','scope':'summary'}}]},
                       {'node':'p','time':100,'conflicts':['a']},{'a':{'node':'p'}},now=100)
        self.assertEqual(task['children'][0]['status'],'conflict')
        self.assertEqual(task['progress']['percent'],0)

    def test_generic_title_and_measured_subtask_progress(self):
        from task_registry import task_view
        task=task_view({'id':'other','name':'User supplied experiment','children':[
            {'id':'one','name':'Compare reference','status':'running','completed_units':25,'total_units':100},
            {'id':'two','name':'Summarize','status':'queued'}]})
        self.assertEqual(task['name'],'User supplied experiment')
        self.assertEqual(task['children'][0]['percent'],25)
        self.assertIsNone(task['children'][1]['percent'])

    def test_three_levels_count_leaf_work_once(self):
        from task_registry import task_view
        task=task_view({'name':'Experiment','children':[
            {'name':'Matching','status':'running','children':[
                {'name':'Chromosome 1','status':'completed'},
                {'name':'Chromosome 2','status':'running'}]},
            {'name':'Summary','status':'queued'}]})
        self.assertEqual(task['progress']['total'],3)
        self.assertEqual(task['progress']['percent'],33.3)
        self.assertEqual(task['children'][0]['percent'],50)

    def test_scan_progress_is_weighted_by_bytes_not_number_of_files(self):
        from task_progress import aggregate
        result=aggregate([{'status':'completed','total_units':1,'unit':'input bytes'},
                          {'status':'running','total_units':99,'completed_units':49,'unit':'input bytes'}])
        self.assertEqual(result['percent'],50)
        self.assertEqual(result['work_percent'],50)
        self.assertEqual(result['completed_units'],50)
        self.assertFalse(result['lower_bound'])
        result=aggregate([{'status':'completed','total_units':1,'unit':'input bytes'},
                          {'status':'running','total_units':999,'completed_units':None,'unit':'input bytes'}])
        self.assertEqual(result['work_percent'],.1)
        self.assertTrue(result['lower_bound'])

    def test_stale_and_wrong_run_measurements_are_not_credited(self):
        manifest={'id':'run','jobs':[{'id':'a','preferred_worker':'peer','spec':{'kind':'bam-read-qc','path':'reads.bam','scope':'QC','bytes':100}}]}
        state={'node':'peer','time':100,'current_job':'a'}
        metric={'run':'run','worker':'peer','sampled_at':99,'completed_units':75,'total_units':100}
        task=peer_task(manifest,state,{},now=100,measurements={'a':metric})
        self.assertEqual(task['children'][0]['percent'],75)
        self.assertEqual(task['progress']['work_percent'],75)
        metric['sampled_at']=1
        task=peer_task(manifest,state,{},now=100,measurements={'a':metric})
        self.assertIsNone(task['children'][0]['percent'])
        metric.update(sampled_at=99,run='another')
        self.assertIsNone(peer_task(manifest,state,{},now=100,measurements={'a':metric})['children'][0]['percent'])

    def test_failed_attempt_is_red_state_until_actually_retrying_or_verified(self):
        manifest={'id':'run','jobs':[{'id':'a','preferred_worker':'peer','spec':{'kind':'bam-read-qc','path':'reads.bam','scope':'QC','bytes':100}}]}
        state={'node':'peer','time':100,'current_job':None}
        failed=peer_task(manifest,state,{},now=100,failures={'a':2})
        self.assertEqual(failed['children'][0]['status'],'failed')
        self.assertEqual(failed['progress']['work_percent'],0)
        state['current_job']='a'
        running=peer_task(manifest,state,{},now=100,failures={'a':2})
        self.assertEqual(running['children'][0]['status'],'running')
        self.assertIn('Retrying',running['children'][0]['detail'])
        completed=peer_task(manifest,state,{'a':{'node':'peer'}},now=100,failures={'a':2})
        self.assertEqual(completed['children'][0]['status'],'completed')

    def test_stage_status_follows_verified_children_and_exposes_failures(self):
        from task_registry import task_view
        task=task_view({'status':'preparing','children':[
            {'name':'Inputs','status':'planning','children':[{'status':'completed'}]},
            {'name':'Mapping','status':'planning','children':[{'status':'failed','detail':'memory'}]}]})
        self.assertEqual(task['children'][0]['status'],'completed')
        self.assertEqual(task['children'][1]['status'],'failed')
        self.assertEqual(task['status'],'failed')

    def test_active_siblings_keep_workflow_running_without_hiding_failure(self):
        from task_registry import task_view
        task=task_view({'status':'failed','children':[
            {'id':'comparison','children':[
                {'id':'done','status':'completed'},
                {'id':'broken','status':'failed'},
                {'id':'busy','status':'running'}]},
            {'id':'report','status':'queued'}]})
        self.assertEqual(task['status'],'running')
        self.assertEqual(task['children'][0]['status'],'running')
        self.assertEqual(task['children'][0]['children'][1]['status'],'failed')
        self.assertEqual(task['active_work_items'],1)
        self.assertEqual(task['failed_work_items'],1)
        self.assertEqual(task['progress']['completed'],1)
        task['children'][0]['children'][2]['status']='completed'
        terminal=task_view(task)
        self.assertEqual(terminal['status'],'failed')
        self.assertEqual(terminal['active_work_items'],0)
        self.assertEqual(terminal['failed_work_items'],1)

    def test_stage_eta_uses_verified_timed_jobs_without_extrapolating_workflow(self):
        from task_registry import task_view
        from unittest.mock import patch
        with patch('task_registry.time.time',return_value=200):
            task=task_view({'children':[
                {'id':'mapping','children':[
                    {'status':'completed','attempt_started':100,'elapsed_seconds':50},
                    {'status':'completed','attempt_started':100,'elapsed_seconds':75},
                    {'status':'running','attempt_started':175,'elapsed_seconds':25},
                    {'status':'queued'}]},
                {'id':'report','status':'not scheduled'}]})
        self.assertEqual(task['children'][0]['execution_eta_seconds'],100)
        self.assertNotIn('execution_eta_seconds',task)

class RuntimeSidecarTests(unittest.TestCase):
    def test_only_fresh_measurements_merge_and_never_change_execution_status(self):
        from task_registry import attach_runtime
        from pathlib import Path
        import tempfile,json
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            (root/'work.json').write_text(json.dumps({'sampled_at':190,'completed_units':40,'total_units':100,'status':'completed','worker':'other','execution_eta_seconds':30}))
            task={'id':'parent','children':[{'id':'work','status':'running','worker':'original'}]}
            attach_runtime(task,root,200)
            child=task['children'][0]
            self.assertEqual(child['completed_units'],40)
            self.assertEqual(child['status'],'running')
            self.assertEqual(child['worker'],'original')
            task={'children':[{'id':'work','status':'running'}]}
            attach_runtime(task,root,300)
            self.assertNotIn('execution_eta_seconds',task['children'][0])
            task={'children':[{'id':'work','status':'completed'}]}
            attach_runtime(task,root,200)
            self.assertNotIn('completed_units',task['children'][0])
