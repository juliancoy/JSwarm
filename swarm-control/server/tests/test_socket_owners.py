import time
from datetime import datetime,timezone
import unittest
from socket_owners import parse_owners, SocketOwners

class SocketOwnerTests(unittest.TestCase):
    def test_ipv6_ipv4_and_unconnected_sockets(self):
        rows=parse_owners('tcp ESTAB 0 0 [2601::1]:123 [2606::1]:443 users:(("chrome",pid=42,fd=3))\n'
                          'udp ESTAB 0 0 10.0.0.1:1234 1.1.1.1:443 users:(("python3",pid=43,fd=5))\n'
                          'udp UNCONN 0 0 0.0.0.0:53 0.0.0.0:* users:(("dns",pid=4,fd=2))')
        self.assertEqual(len(rows),2)
        self.assertEqual(rows[(6,'2601::1',123,'2606::1',443)],[{'name':'chrome','pid':42}])

    def test_only_fresh_exact_host_socket_gets_owner(self):
        monitor=SocketOwners();monitor.sampled_at=time.time()
        monitor.remember({(6,'10.0.0.1',123,'1.1.1.1',443):[{'name':'chrome','pid':42}]},time.time())
        flow={'device_mac':'host','protocol':6,'local_ip':'10.0.0.1','local_port':123,'remote_ip':'1.1.1.1','remote_port':443,'last_seen':datetime.now(timezone.utc).isoformat()}
        other={**flow,'device_mac':'other'}
        monitor.annotate([flow,other],'host')
        self.assertEqual(flow['recent_processes'][0]['pid'],42)
        self.assertNotIn('recent_processes',other)
        monitor.history={};monitor.sampled_at=1;fresh={k:v for k,v in flow.items() if k not in ('recent_processes','process_sampled_at')}
        monitor.annotate([fresh],'host');self.assertNotIn('recent_processes',fresh)
