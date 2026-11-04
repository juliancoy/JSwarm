import unittest
from unittest.mock import patch
from work_history import project,snapshot,machine_contribution
import test_api as remote_tests


class HistoryProjectionTests(unittest.TestCase):
    def test_genome_title_counts_and_machines_exclude_private_reports(self):
        row=project({'id':'request','kind':'research','selection':{'research':'baseline'}},
                    {'id':'run','status':'running','progress':{'total':8,'completed':3,'failed':1,'path':'private'},
                     'logs':['private'],'hosts':[{'host':'worker.local','status':'running','secret':'private',
                     'jobs':{'batch':{'name':'DNA batch','status':'running','payload':'private'}}}]})
        self.assertEqual(row['title'],'Genome analysis — baseline')
        self.assertEqual(row['progress'],{'total':8,'completed':3,'failed':1})
        self.assertEqual(row['machines'][0]['tasks'],['DNA batch'])
        self.assertNotIn('private',str(row))

    def test_contributions_distinguish_measured_cpu_estimates_and_reused_work(self):
        host={'jobs':{
            'fresh':{'status':'passed','cpus':2,'duration_seconds':100,'phase_seconds':{'test':4},'cpu_seconds':5},
            'failed':{'status':'failed','cpus':1,'duration_seconds':3},
            'reused':{'status':'reused','cpus':8,'duration_seconds':900,'cpu_seconds':800},
            'cached':{'status':'passed','reused':True,'cpu_seconds':700,'duration_seconds':800,'cpus':1},
            'skipped':{'status':'passed','not_executed':True,'cpu_seconds':600},
            'invalid':{'status':'running','cpu_seconds':float('nan')},
        }}
        row=machine_contribution(host)
        self.assertEqual(row['completed_tasks'],1)
        self.assertEqual(row['reused_tasks'],2)
        self.assertEqual(row['cpu_seconds'],5)
        self.assertEqual(row['estimated_cpu_slot_seconds'],11)
        self.assertEqual(row['cpu_measured_tasks'],1)
        self.assertEqual(row['cpu_estimated_tasks'],2)
        self.assertIsNone(machine_contribution({'jobs':{'old':{'status':'passed'}}})['cpu_seconds'])

    def test_web_and_mixed_scopes_have_distinct_titles(self):
        self.assertEqual(project({'selection':{'suites':['web']}},{})['title'],'Web tests')
        self.assertEqual(project({'selection':{'suites':['visual']}},{})['title'],'Web tests — Web layout tests')
        self.assertIn('Distributed tests',project({'selection':{'suites':['web','org-api']}},{})['title'])
        title=project({'selection':{'paths':['OrgPortal/web/src/views/events.test.ts']}},{})['title']
        self.assertEqual(title,'Web tests — events.test.ts')
        self.assertNotIn('OrgPortal/web',title)

    def test_requests_and_legacy_runs_are_deduplicated_and_active_first(self):
        requests=[{'id':'request','run_id':'20261026T120000Z-1234abcd','status':'passed','submitted_at':'2026-10-26','selection':{'suites':['web']}}]
        completed={'id':'20261026T120000Z-1234abcd','status':'passed','started':'2026-10-26','hosts':[]}
        active={'id':'20261025T120000Z-1234abcd','kind':'research','profile':'baseline','status':'running','started':'2026-10-25','hosts':[]}
        with patch('work_history.requests',return_value=requests),patch('work_history.read_coordinator',return_value={'run':active,'history':[completed]}) as read:
            result=snapshot()
        self.assertEqual(len(result['jobs']),2)
        self.assertEqual(result['jobs'][0]['kind'],'genome')
        self.assertEqual(read.call_count,1)


    def test_older_active_work_is_not_hidden_by_thirty_newer_finished_requests(self):
        records=[{'id':str(n),'status':'passed','submitted_at':f'2026-10-26T12:00:{n:02}'} for n in range(35)]
        records.append({'id':'active','kind':'research','selection':{'research':'baseline'},'status':'running','submitted_at':'2026-10-25'})
        with patch('work_history.requests',return_value=records),patch('work_history.read_coordinator',return_value={}):
            result=snapshot()
        self.assertEqual(len(result['jobs']),30)
        self.assertEqual(result['jobs'][0]['id'],'active')


class HistoryBoundaryTests(unittest.TestCase):
    setUp=remote_tests.RemoteBoundaryTests.setUp
    request=remote_tests.RemoteBoundaryTests.request
    def test_history_requires_session_and_is_read_only(self):
        for path in ['/history','/history.html','/history.js','/history.css']:
            self.assertEqual(self.request(path),200,path)
        with patch('work_history.snapshot',return_value={'jobs':[]}) as read:
            self.assertEqual(self.request('/api/v1/swarm/history'),401)
            self.assertEqual(self.request('/api/v1/swarm/history',cookie=self.cookie,origin='https://evil.example'),403)
            self.assertEqual(self.request('/api/v1/swarm/history','POST',cookie=self.cookie,origin='https://compai.example',body={}),404)
            read.assert_not_called()
            self.assertEqual(self.request('/api/v1/swarm/history',cookie=self.cookie),200)
            read.assert_called_once()
