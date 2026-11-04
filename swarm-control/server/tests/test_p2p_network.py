import copy
import unittest
import threading
from unittest.mock import Mock
from p2p_network import direct_evidence, NetworkP2P
from distributed import plan
from distributed_images import required_images


class NetworkEvidenceTests(unittest.TestCase):
    def test_sources_use_published_owner_commit_in_split_workspace(self):
        from unittest.mock import patch
        network=NetworkP2P([], 'run',source_commit='a'*40,source_repositories={'JSwarm':{'commit':'b'*40}})
        with patch('p2p_network.subprocess.check_output',return_value=b'reviewed') as read:
            self.assertEqual(network.source('peer-worker/public/peer.js'),b'reviewed')
        self.assertTrue(read.call_args.args[0][2].endswith('/JSwarm'))
        self.assertEqual(read.call_args.args[0][-1],'b'*40+':peer-worker/public/peer.js')

    def test_cleanup_removes_owned_containers_concurrently_even_if_one_fails(self):
        network=NetworkP2P([], 'run')
        barrier=threading.Barrier(2)
        def stop(*args,**kwargs):barrier.wait(timeout=2);return {'acknowledged':True}
        healthy=Mock();healthy.call.side_effect=stop
        attempts=[]
        def offline(*args,**kwargs):
            if not attempts:barrier.wait(timeout=2)
            attempts.append(1)
            raise OSError('offline')
        dead=Mock();dead.call.side_effect=offline
        browsers=[Mock(),Mock()]
        network.resources=[{'host':{'host':name},'client':client,'browser':browser}
                           for name,client,browser in zip(['healthy','dead'],[healthy,dead],browsers)]
        network.cleanup()
        self.assertEqual(network.report['cleanup_errors'],['dead'])
        healthy.call.assert_called_once_with('p2p.stop',{'id':'run'},timeout=40)
        self.assertEqual(dead.call.call_count,2)
        for browser in browsers:browser.close.assert_not_called()

    def test_cleanup_retries_an_uncertain_acknowledgement_without_losing_success(self):
        client=Mock();client.call.side_effect=[TimeoutError('slow removal'),{'acknowledged':True}]
        network=NetworkP2P([], 'run')
        network.resources=[{'host':{'host':'peer'},'client':client,'browser':None}]
        network.cleanup()
        self.assertNotIn('cleanup_errors',network.report)
        self.assertEqual(client.call.call_count,2)
        client.close.assert_called_once()

    def fixture(self):
        participants=[{'host':'a.local','machine':'machine-a','peer':'a','addresses':['10.42.0.10']},
                      {'host':'b.local','machine':'machine-b','peer':'b','addresses':['10.42.0.11']}]
        links=[{'peer':'b','state':'connected','channels':[{'bytesSent':1200000,'bytesReceived':50}],
                'pair':{'state':'succeeded','bytesSent':1200000,'bytesReceived':50,
                        'local':{'address':'10.42.0.10','candidateType':'host','protocol':'udp'},
                        'remote':{'address':'10.42.0.11','candidateType':'host','protocol':'udp'}}}]
        return participants,links

    def test_real_attested_network_path_is_accepted(self):
        participants,links=self.fixture()
        self.assertEqual(direct_evidence(links,participants,'a.local'),links)

    def test_relay_loopback_hidden_addresses_wrong_hosts_and_no_traffic_cannot_pass(self):
        original,original_links=self.fixture()
        cases=[('remote','candidateType','relay'),('local','address','127.0.0.1'),
               ('remote','address','::1'),('remote','address','hidden.local'),
               ('remote','address','10.42.0.99'),('local','candidateType',None)]
        for candidate,key,value in cases:
            links=copy.deepcopy(original_links);links[0]['pair'][candidate][key]=value
            with self.subTest(candidate=candidate,key=key,value=value),self.assertRaises(RuntimeError):
                direct_evidence(links,original,'a.local')
        links=copy.deepcopy(original_links);links[0]['pair'].update(bytesSent=0,bytesReceived=0)
        with self.assertRaises(RuntimeError):direct_evidence(links,original,'a.local')
        participants=copy.deepcopy(original);participants[1]['machine']=participants[0]['machine']
        with self.assertRaises(RuntimeError):direct_evidence(original_links,participants,'a.local')
        with self.assertRaises(RuntimeError):direct_evidence([],original,'a.local')
        with self.assertRaises(RuntimeError):direct_evidence(original_links,original[:1],'a.local')

    def test_network_suite_is_group_coordinated_and_its_image_is_distributed(self):
        for host in plan([{'host':'a.local','user':'julian'},{'host':'b.local','user':'julian'}]):
            self.assertIn('p2p',host['suites'])
            self.assertIn('selenium/standalone-chrome:latest',required_images(host))


if __name__=='__main__':unittest.main()


def test_browser_admission_retries_authenticated_docker_inspection_timeout(monkeypatch,tmp_path):
    import agent_client
    import p2p_network
    starts=[]
    class Client:
        def __init__(self,host):pass
        def upload(self,*args):return 'upload-id'
        def call(self,op,args,**kwargs):
            if op=='p2p.start':
                starts.append(args)
                if len(starts)==1:raise RuntimeError("TimeoutExpired: Command ['docker', 'inspect', 'worker'] timed out")
                return {'started':True}
            if op=='p2p.webdriver':return {'value':{'ready':True}}
    monkeypatch.setattr(agent_client,'AgentClient',Client)
    monkeypatch.setattr(p2p_network.time,'sleep',lambda _:None)
    monkeypatch.setattr(p2p_network,'Browser',lambda *a:__import__('types').SimpleNamespace(version='fixture'))
    network=NetworkP2P([], 'test-run');network.report['browser_images']={'peer':{p2p_network.IMAGE:'sha256:pinned'}}
    network.prepare({'host':'peer'},tmp_path/'fixture.tgz')
    assert len(starts)==2 and starts[0]==starts[1]


def test_status_attestation_retries_timeout_without_using_stale_status(monkeypatch):
    import agent_client,p2p_network,time
    calls=[];closed=[]
    class Client:
        def __init__(self,host):pass
        def call(self,op,**kwargs):
            assert kwargs.get('timeout')==150
            calls.append(op)
            if len(calls)==1:raise TimeoutError('busy')
            return {'addresses':['10.42.0.2'],'time':time.time(),'machine':'verified-machine'}
        def close(self):closed.append(True)
    monkeypatch.setattr(agent_client,'AgentClient',Client)
    monkeypatch.setattr(p2p_network.time,'sleep',lambda _:None)
    value=NetworkP2P([],'test-run').attest({'host':'peer'})
    assert value['machine']=='verified-machine' and len(calls)==2 and len(closed)==2
