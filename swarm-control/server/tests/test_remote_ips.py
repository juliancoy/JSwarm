import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from remote_ips import RemoteIPs, lookup, reverse_dns


class RemoteIPTests(unittest.TestCase):
    def test_public_only_cached_and_persistent(self):
        with tempfile.TemporaryDirectory() as directory, patch('remote_ips.lookup',return_value={'ip':'104.21.15.38','asn':13335,'cached_at':time.time(),'dns_cached_at':time.time(),'dns_engine':'dig','dns_ttl':300,'organization':'Cloudflare, Inc.'}) as resolver:
            path=Path(directory)/'remote.json'
            service=RemoteIPs(path)
            self.assertFalse(service.start('10.42.0.1'))
            self.assertFalse(service.start('https://example.com'))
            self.assertTrue(service.start('104.21.15.38'))
            service.queue.join()
            self.assertTrue(service.start('104.21.15.38'))
            self.assertEqual(resolver.call_count,1)
            self.assertEqual(RemoteIPs(path).view()['104.21.15.38']['asn'],13335)

    def test_no_reverse_record_is_explicit_and_does_not_invent_a_domain(self):
        with patch('remote_ips.subprocess.run') as run, patch('remote_ips.urllib.request.urlopen',side_effect=OSError):
            run.return_value.returncode=0
            run.return_value.stdout=';; ->>HEADER<<- opcode: QUERY, status: NXDOMAIN, id: 1'
            record=lookup('104.21.15.38')
            self.assertIsNone(record['ptr'])
            self.assertEqual(record['dns_status'],'NXDOMAIN')
            self.assertIn('No PTR record',record['ptr_status'])
            self.assertIn('network_error',record)

    def test_ptr_answers_and_dns_failures_are_distinct(self):
        with patch('remote_ips.subprocess.run') as run:
            run.return_value.returncode=0
            run.return_value.stdout=';; ->>HEADER<<- opcode: QUERY, status: NOERROR, id: 1\n1.1.1.1.in-addr.arpa. 120 IN PTR one.one.one.one.\n1.1.1.1.in-addr.arpa. 60 IN PTR alternate.example.\n;; SERVER: 127.0.0.53#53 (UDP)'
            result=reverse_dns('1.1.1.1')
            self.assertEqual(result['ptr_names'],['one.one.one.one','alternate.example'])
            self.assertEqual(result['dns_ttl'],60)
            self.assertEqual(result['dns_status'],'NOERROR')
            run.return_value.stdout=';; ->>HEADER<<- opcode: QUERY, status: SERVFAIL, id: 1'
            self.assertIn('failed',reverse_dns('1.1.1.1')['ptr_status'])
            self.assertEqual(run.call_args.args[0][0],'dig')

    def test_failed_lookup_does_not_stop_following_ipv6_resolution(self):
        with tempfile.TemporaryDirectory() as directory, patch('remote_ips.lookup',side_effect=[OSError('fixture failure'),{'ip':'2606:4700:4700::1111','ptr':'one.one.one.one','dns_engine':'dig','dns_ttl':300,'dns_cached_at':time.time()}]):
            service=RemoteIPs(Path(directory)/'remote.json')
            service.start('2606:4700:4700::1001')
            service.start('2606:4700:4700::1111')
            service.queue.join()
            self.assertTrue(service.diagnostics()['worker_alive'])
            self.assertEqual(service.view()['2606:4700:4700::1111']['ptr'],'one.one.one.one')
