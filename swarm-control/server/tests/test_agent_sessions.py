import hashlib
from pathlib import Path
import unittest
import tempfile
from unittest.mock import Mock,patch
from agent_client import AgentSessions


class AgentReadinessTests(unittest.TestCase):
    def setUp(self):
        from agent_updates import LauncherUpdates
        folder=tempfile.TemporaryDirectory();self.addCleanup(folder.cleanup)
        root=Path(__file__).resolve().parents[3]
        updater=LauncherUpdates(root,lambda:False,Mock())
        updater.git_source={'repositories':{name:{'origin':'https://example.com/'+name+'.git','commit':'a'*40}
                            for name in ('Organization','JSwarm','HarborGenetics')},'installer':'# test Git installer\n'}
        artifact=updater.publish(folder.name)
        publication=LauncherUpdates.published(artifact,lambda:False,Mock())
        selected=patch('agent_updates.LauncherUpdates.desired',return_value=publication)
        selected.start();self.addCleanup(selected.stop)

    def test_busy_compatible_agent_remains_eligible_but_missing_capabilities_do_not(self):
        root=Path(__file__).resolve().parents[3]
        required=['github-jobs','p2p-browser','cancel','events','layer-pull','registry-read','peer-layer-cache','mptcp-transport','file-queue','snapshot-stage','event-wait','shared-resources','controller-signature']
        for capabilities,eligible in ((required,True),(required[:-1],False)):
            with self.subTest(eligible=eligible):
                host={'host':'a.local','lan_address':'10.42.0.1'}
                status={'protocol':'lifetech-agent.v1','ready':True,'source_revision':'previous',
                        'capabilities':capabilities,'addresses':['10.42.0.1'],
                        'machine':'machine-a','active_jobs':['existing-job']}
                client=Mock();client.call.return_value=status
                service=AgentSessions(root,[host],lambda:False,Mock())
                with patch('agent_client.AgentClient',return_value=client),patch('agent_updates.LauncherUpdates.stage') as stage:
                    self.assertEqual(bool(service.verify([host])),eligible)
                stage.assert_not_called()

    def test_offline_node_does_not_exclude_healthy_agent(self):
        root=Path(__file__).resolve().parents[3]
        hosts=[{'host':'a.local','lan_address':'10.42.0.1'},{'host':'b.local','lan_address':'10.42.0.2'}]
        folder=root/'scripts'
        from agent_updates import LauncherUpdates
        revision=LauncherUpdates(root,lambda:False,Mock()).revision
        status={'protocol':'lifetech-agent.v1','ready':True,'source_revision':revision,
                'capabilities':['github-jobs','p2p-browser','cancel','events','layer-pull','registry-read','peer-layer-cache','mptcp-transport','file-queue','snapshot-stage','event-wait','shared-resources','controller-signature'],
                'addresses':['10.42.0.1'],'machine':'machine-a','active_jobs':[]}
        def client(host):
            result=Mock()
            if host['host']=='a.local':result.call.return_value=status
            else:result.call.side_effect=TimeoutError('offline')
            return result
        service=AgentSessions(root,hosts,lambda:False,Mock())
        with patch('agent_client.AgentClient',side_effect=client):ready=service.verify(hosts)
        self.assertEqual(ready,[hosts[0]])
        self.assertTrue(hosts[0]['session']['verified'])
        self.assertIn('offline',hosts[1]['error'])

    def test_cancellation_does_not_contact_an_agent(self):
        service=AgentSessions('.',[],lambda:True,Mock())
        with patch('agent_client.AgentClient') as client:
            with self.assertRaises(InterruptedError):service.verify([{'host':'a.local'}])
        client.assert_not_called()

class WANDiscoveryTests(unittest.TestCase):
    def test_approved_remote_address_uses_webrtc_and_requires_machine_pin(self):
        import json
        from agent_client import AgentSessions
        with tempfile.TemporaryDirectory() as directory:
            config=Path(directory)/'peers.json'
            value={'name':'master.local','peers':{'master.local':'10.42.0.1','remote.local':'192.168.5.4','unapproved.local':'192.168.5.5'},
                   'wan_only':['remote.local','unapproved.local'],'internet_nodes':{'remote.local':'peer','unapproved.local':'other'},'enrolled_machines':{'remote.local':'machine'}}
            config.write_text(json.dumps(value))
            service=AgentSessions(Path(__file__).resolve().parents[3],[{'host':'remote.local'},{'host':'unapproved.local'}],lambda:False,Mock())
            interfaces=[{'addr_info':[{'local':'10.42.0.1','prefixlen':24,'scope':'global'}]}]
            with patch('agent_client.CONFIG',config),patch('subprocess.check_output',return_value=json.dumps(interfaces)),patch.dict('sys.modules',{'zeroconf':Mock(),'launcher_discovery':Mock(SERVICE='_lifetech._tcp.local.')}),patch('agent_client.time.monotonic',side_effect=[0,5]):
                hosts=service.accessible()
            self.assertEqual(len(hosts),1)
            self.assertEqual(hosts[0]['agent_route'],'webrtc')
            self.assertEqual(hosts[0]['expected_machine'],'machine')
            self.assertEqual(hosts[0]['agent_endpoint'],'http://127.0.0.1:8791')
