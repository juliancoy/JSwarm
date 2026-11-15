"""Host tests for bounded direct payload framing and dual-stack routing."""
import io
import json
import socket
import struct
import unittest
from unittest.mock import Mock,patch
from scripts import swarm_payload as payload


class PayloadTests(unittest.TestCase):
    def test_local_command_and_remote_enrollment(self):
        self.assertEqual(payload.command({'local':True},['cat'],direction='upload'),['cat'])
        with self.assertRaises(ValueError):payload.command({'host':'node.local'},['cat'],direction='upload')
        argv=payload.command({'host':'node.local','machine':'a'*64,'agent_endpoint':'endpoint'},['printf','$(false)'],direction='download')
        self.assertNotIn('ssh',argv)
        self.assertEqual(argv[-2:],['printf','$(false)'])

    def test_frame_bounds_and_truncation(self):
        connection=Mock();connection.recv.side_effect=[struct.pack('!I',3),b'a',b'bc']
        self.assertEqual(payload.receive(connection),b'abc')
        connection.recv.side_effect=[struct.pack('!I',payload.BLOCK+1)]
        with self.assertRaises(ValueError):payload.receive(connection)
        connection.recv.side_effect=[b'']
        with self.assertRaises(ConnectionError):payload.receive(connection)
        with self.assertRaises(ValueError):payload.send(connection,b'x'*(payload.BLOCK+1))

    def test_failed_ipv6_falls_back_to_ipv4_without_losing_scope(self):
        v6=('fe80::1',4444,0,7);v4=('192.0.2.1',4444)
        first=Mock();first.connect.side_effect=OSError('unreachable');second=Mock()
        with patch.object(payload.socket,'getaddrinfo',return_value=[(socket.AF_INET6,1,6,'',v6),(socket.AF_INET,1,6,'',v4)]),patch.object(payload,'transport_socket',side_effect=[first,second]):
            self.assertIs(payload.connect('node.local',4444),second)
        first.connect.assert_called_once_with(v6);first.close.assert_called_once();second.connect.assert_called_once_with(v4)

    def test_mptcp_unsupported_falls_back_to_tcp(self):
        import errno
        with patch.object(payload.socket,'socket',side_effect=[OSError(errno.EPROTONOSUPPORT,'unsupported'),Mock()]) as create:
            payload.transport_socket(socket.AF_INET6)
        self.assertEqual(create.call_args_list[-1].args,(socket.AF_INET6,socket.SOCK_STREAM))

    def test_hostname_certificate_has_dns_identity(self):
        from cryptography import x509
        certificate,key=payload.certificate('node.local')
        cert=x509.load_pem_x509_certificate(certificate.encode())
        self.assertEqual(cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value.get_values_for_type(x509.DNSName),['node.local'])


class RemoteIdentityTests(unittest.TestCase):
    def test_remote_rejects_wrong_machine_before_binding_or_running_command(self):
        import subprocess,sys
        result=subprocess.run([sys.executable,'-c',payload.REMOTE],input=json.dumps({'machine':'0'*64}),capture_output=True,text=True)
        self.assertNotEqual(result.returncode,0)
        self.assertIn('Payload machine identity changed',result.stderr)
        self.assertEqual(result.stdout,'')


class RouteSelectionTests(unittest.TestCase):
    def test_slow_reachable_address_does_not_hide_fast_route(self):
        import threading,time
        slow=Mock();fast=Mock();finished=threading.Event()
        slow.connect.side_effect=lambda address:time.sleep(.1)
        slow.close.side_effect=lambda:finished.set()
        def socket_for(family):return slow if family==socket.AF_INET6 else fast
        candidates=[(socket.AF_INET6,1,6,'',('fe80::1',4444,0,7)),(socket.AF_INET,1,6,'',('192.0.2.1',4444))]
        with patch.object(payload.socket,'getaddrinfo',return_value=candidates),patch.object(payload,'transport_socket',side_effect=socket_for):
            self.assertIs(payload.connect('node.local',4444),fast)
            self.assertTrue(finished.wait(1))
        fast.close.assert_not_called();slow.close.assert_called_once()
