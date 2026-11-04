import io
import json
import unittest
from unittest.mock import patch
import test_api as remote_tests
import devices
import status


class RemoteStatusBoundaryTests(unittest.TestCase):
    setUp = remote_tests.RemoteBoundaryTests.setUp
    request = remote_tests.RemoteBoundaryTests.request

    def test_status_requires_owner_or_paired_session_and_forbids_writes(self):
        with patch('status.snapshot', return_value={'devices': [], 'run': {}}) as snapshot:
            self.assertEqual(self.request('/api/v1/swarm/status'), 401)
            self.assertEqual(self.request('/api/v1/swarm/status', 'POST', 'owner'*16, 'https://compai.example', body={}), 404)
            snapshot.assert_not_called()
            self.assertEqual(self.request('/api/v1/swarm/status', token='owner'*16), 401)
            cookie=self.cookie
            self.assertEqual(self.request('/api/v1/swarm/status', cookie=cookie), 200)
            self.assertEqual(self.request('/api/v1/swarm/status', cookie=cookie, origin='https://evil.example'), 403)

    def test_unavailable_coordinator_returns_retryable_error(self):
        with patch('status.snapshot', side_effect=OSError('private upstream detail')):
            self.assertEqual(self.request('/api/v1/swarm/status', cookie=self.cookie), 503)


class SnapshotProjectionTests(unittest.TestCase):
    def test_fixed_read_sources_exclude_private_coordinator_fields(self):
        network={'updated':'2026-10-24T20:00:00Z','devices':[{'hostname':'worker.local','ip':'10.0.0.2','ping':True,'mac':'private','ports':[22]}]}
        tests={'run':{'id':'fixture','status':'running','hosts':[{'host':'worker.local','status':'running','session':{'secret':'private'},'logs':['private'], 'jobs':{'test':{'name':'Fixture test','status':'running','logs':['private']}}}]}}
        with patch('status.urlopen', side_effect=[io.BytesIO(json.dumps(network).encode()),io.BytesIO(json.dumps(tests).encode())]) as fetch:
            result=status.snapshot()
        self.assertEqual([call.args[0].full_url for call in fetch.call_args_list],['http://127.0.0.1:8089/api/devices','http://127.0.0.1:8089/api/distributed-tests'])
        self.assertNotIn('private',json.dumps(result))
        self.assertTrue(result['devices'][0]['reachable'])
        self.assertEqual(result['run']['hosts'][0]['jobs'][0]['status'],'running')

class TaskManagerProjectionTests(unittest.TestCase):
    def test_normal_large_snapshot_is_projected_without_forwarding_flow_details(self):
        network={'devices':[], 'flow_details':'private'*(700000)}
        with patch('status.urlopen',side_effect=[io.BytesIO(json.dumps(v).encode()) for v in [network,{},{},{'hosts':[],'history':[]}]]):
            result=status.task_manager()
        self.assertNotIn('private',json.dumps(result))
        self.assertEqual(result['network']['devices'],[])

    def test_oversized_coordinator_snapshot_still_has_a_hard_read_bound(self):
        stream=io.BytesIO(b' '*(status.MAX_COORDINATOR_SNAPSHOT_BYTES+1))
        with patch('status.urlopen',return_value=stream):
            with self.assertRaisesRegex(ValueError,'size limit'):
                status.read_coordinator('/api/devices')

    def test_remote_resources_preserve_measurements_and_exclude_credentials(self):
        network={'devices':[{'ip':'10.0.0.2','mac':'aa','hostname':'worker.local','ports':[22]}],
                 'link_traffic':{'updated':'now','peers':{'aa':{'rx_mbps':3,'tx_mbps':2,'secret':'private'}}},
                 'docker_downloads':{'status':'available','active':[{'image':'node:22','pid':42,'command':'private'}]},
                 'process_resources':{'command':'private'}}
        metrics={'total_percent':42,'live_hosts':1,'hosts':[{'host':'worker.local','status':'live','percent':42,
                 'sampled_at':123,'network_interfaces':[{'name':'eth0','rx_mbps':2,'tx_mbps':3,'secret':'private'}],'ram_percent':40,'disk_busy_percent':25,'network_mbps':5,
                 'session':{'token':'private'},'hardware':{'storage':{'path':'private'},'gpus':[{'name':'GPU','utilization_percent':12,'memory_total_mib':100,'memory_free_mib':50,'secret':'private'}]}}]}
        metrics['history']=[{'sampled_at':123,'partial':True,'hosts':[metrics['hosts'][0]],'network':network}]
        tests={'run':{'id':'fixture','status':'running','hosts':[],'credentials':'private'}}
        with patch('status.urlopen',side_effect=[io.BytesIO(json.dumps(v).encode()) for v in [network,metrics,tests,{'hosts':[],'history':[]}]]) as fetch:
            result=status.task_manager()
        self.assertEqual([call.args[0].full_url for call in fetch.call_args_list],['http://127.0.0.1:8089/api/devices?resolve=0','http://127.0.0.1:8089/api/cpu','http://127.0.0.1:8089/api/distributed-tests','http://127.0.0.1:8795/api/resources'])
        self.assertTrue(result['metrics']['history'][0]['partial'])
        host=result['metrics']['hosts'][0]
        for key in ['percent','ram_percent','disk_busy_percent','network_mbps']:
            self.assertEqual(host[key],metrics['hosts'][0][key])
        self.assertEqual(host['hardware']['gpus'][0]['utilization_percent'],12)
        self.assertEqual(result['network']['link_traffic']['peers']['aa']['rx_mbps'],3)
        self.assertEqual(result['metrics']['history'][0]['hosts'][0]['percent'],42)
        self.assertEqual(host['network_interfaces'][0]['name'],'eth0')
        self.assertEqual(result['metrics']['history'][0]['hosts'][0]['network_interfaces'][0]['rx_mbps'],2)
        self.assertEqual(result['metrics']['history'][0]['network']['link_traffic']['peers']['aa']['rx_mbps'],3)
        self.assertNotIn('private',json.dumps(result))

    def test_live_monitor_details_and_history_reach_task_manager(self):
        resources={'hosts':[{'host':'worker.local','performance':{'schema_version':1,'processes':[{'pid':7,'rss_bytes':4096}],'memory':{'dram_read_bytes_per_second':123}}}],
                   'history':[{'sampled_at':123,'hosts':[]}],'credential':'private'}
        with patch('status.urlopen',side_effect=[io.BytesIO(json.dumps(v).encode()) for v in [{},{},{},resources]]):
            result=status.task_manager()
        self.assertEqual(result['resources']['hosts'],resources['hosts'])
        self.assertEqual(result['resources']['history'],resources['history'])
        self.assertNotIn('credential',result['resources'])

    def test_monitor_outage_does_not_hide_tasks(self):
        with patch('status.read_coordinator',return_value={}),patch('status.read_resources',side_effect=OSError('offline')):
            result=status.task_manager()
        self.assertEqual(result['resources']['status'],'unavailable')
        self.assertIn('run',result)

    def test_resource_feed_is_bounded(self):
        with patch('status.urlopen',return_value=io.BytesIO(b' '*(4*1024*1024+1))):
            with self.assertRaises(ValueError):status.task_manager()

class RemoteTaskManagerBoundaryTests(RemoteStatusBoundaryTests):
    def test_task_manager_and_resources_are_available_to_paired_owner(self):
        for path in ('/task-manager','/task-manager.html','/task-manager.js','/task-manager.css','/levels.js','/levels-model.js','/cpu-report.js','/snapshot-fetch.js','/docker-downloads.js','/tests.css','/monitor.js','/monitor.css','/monitor-model.js'):
            self.assertEqual(self.request(path),200,path)
        with patch('status.task_manager',return_value={'network':{},'metrics':{},'run':{}}) as feed:
            path='/api/v1/swarm/task-manager'
            self.assertEqual(self.request(path),401)
            self.assertEqual(self.request(path,origin='https://evil.example',cookie=self.cookie),403)
            self.assertEqual(self.request(path,'POST',origin='https://compai.example',body={},cookie=self.cookie),404)
            feed.assert_not_called()
            self.assertEqual(self.request(path,cookie=self.cookie),200)
            feed.assert_called_once()
        with patch('status.task_manager',side_effect=OSError('private')):
            self.assertEqual(self.request('/api/v1/swarm/task-manager',cookie=self.cookie),503)
