import hashlib
import json
from pathlib import Path
import ssl
import subprocess
import tempfile
import threading
import unittest
import urllib.request
from scripts.swarm_tls_transfer import byte_range, fetch, identity, make_server


class TransferTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(); cls.root = Path(cls.temp.name)
        def openssl(*args):
            subprocess.run(['openssl', *args], cwd=cls.root, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        openssl('req', '-x509', '-newkey', 'ec', '-pkeyopt', 'ec_paramgen_curve:P-256', '-nodes', '-keyout', 'ca.key', '-out', 'ca.crt', '-subj', '/CN=test-ca', '-days', '1', '-addext', 'basicConstraints=critical,CA:TRUE', '-addext', 'keyUsage=critical,keyCertSign,cRLSign')
        for name, purpose in [('server', 'serverAuth'), ('client', 'clientAuth')]:
            openssl('req', '-new', '-newkey', 'ec', '-pkeyopt', 'ec_paramgen_curve:P-256', '-nodes', '-keyout', name+'.key', '-out', name+'.csr', '-subj', '/CN='+name)
            (cls.root/(name+'.ext')).write_text('basicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature\nextendedKeyUsage='+purpose+'\nsubjectAltName=IP:127.0.0.1\n')
            openssl('x509', '-req', '-in', name+'.csr', '-CA', 'ca.crt', '-CAkey', 'ca.key', '-CAcreateserial', '-out', name+'.crt', '-days', '1', '-extfile', name+'.ext')
        cls.data = bytes(range(256)) * 8192
        source = cls.root/'source'; source.write_bytes(cls.data)
        cls.sha = hashlib.sha256(cls.data).hexdigest()
        (cls.root/'objects.json').write_text(json.dumps({'sample':{'path':str(source),'sha256':cls.sha,'identity':identity(source.stat())}}))
        cls.server = make_server('127.0.0.1', 0, cls.root/'objects.json', cls.root/'server.crt', cls.root/'server.key', cls.root/'ca.crt')
        cls.url = 'https://127.0.0.1:'+str(cls.server.server_port)+'/objects/sample'
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True); cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown(); cls.server.server_close(); cls.thread.join(); cls.temp.cleanup()

    def context(self, client=True):
        ctx=ssl.create_default_context(cafile=str(self.root/'ca.crt'))
        if client:ctx.load_cert_chain(self.root/'client.crt', self.root/'client.key')
        return ctx

    def test_checksum_progress_counts_checked_bytes_separately_from_transfer(self):
        from unittest.mock import patch
        dest=self.root/'progress-copy';samples=[]
        with patch('scripts.swarm_tls_transfer.publish_progress',side_effect=lambda *a,**kw:samples.append((a,kw))):
            fetch(self.url,dest,self.sha,len(self.data),self.root/'ca.crt',self.root/'client.crt',self.root/'client.key',metadata={'id':'test'})
        checked=[args[2] for args,kw in samples if args[1]=='verifying']
        self.assertEqual(checked[0],0)
        self.assertEqual(checked[-1],len(self.data))
        self.assertEqual(samples[-1][0][1],'complete')

    def test_requires_client_certificate(self):
        with self.assertRaises(Exception):
            urllib.request.urlopen(self.url, context=self.context(False))

    def test_resume_and_verify(self):
        dest=self.root/'resume'/'copy'; dest.parent.mkdir()
        partial=dest.parent/'.rsync-partial'; partial.mkdir(); (partial/'copy').write_bytes(self.data[:76543])
        fetch(self.url, dest, self.sha, len(self.data), self.root/'ca.crt', self.root/'client.crt', self.root/'client.key')
        self.assertEqual(dest.read_bytes(), self.data)
        self.assertEqual(dest.stat().st_mode & 0o777, 0o600)
        self.assertFalse((partial/'copy').exists())

    def test_corrupt_partial_never_published(self):
        dest=self.root/'corrupt'/'copy'; dest.parent.mkdir()
        partial=dest.parent/'.rsync-partial'; partial.mkdir(); (partial/'copy').write_bytes(b'x'*76543)
        with self.assertRaisesRegex(ValueError, 'checksum'):
            fetch(self.url, dest, self.sha, len(self.data), self.root/'ca.crt', self.root/'client.crt', self.root/'client.key')
        self.assertFalse(dest.exists())

    def test_allowlist_and_source_identity(self):
        for path, status in [('/objects/../source',404),('/objects/absent',404)]:
            with self.assertRaises(urllib.error.HTTPError) as error:
                urllib.request.urlopen(self.url.split('/objects/')[0]+path, context=self.context())
            self.assertEqual(error.exception.code,status)
        source=self.root/'source'; source.touch()
        try:
            with self.assertRaises(urllib.error.HTTPError) as error:
                urllib.request.urlopen(self.url,context=self.context())
            self.assertEqual(error.exception.code,409)
        finally:
            manifest=self.root/'objects.json'
            rows=json.loads(manifest.read_text()); rows['sample']['identity']=identity(source.stat())
            manifest.write_text(json.dumps(rows))

    def test_manifest_directory_requires_destination_certificate_acl(self):
        source=self.root/'acl-source'; source.write_bytes(b'allowlisted synthetic object')
        folder=self.root/'acl-objects';folder.mkdir()
        row={'path':str(source),'sha256':hashlib.sha256(source.read_bytes()).hexdigest(),'identity':identity(source.stat()),'clients':[]}
        entry=folder/'sample.json';entry.write_text(json.dumps(row))
        server=make_server('127.0.0.1',0,folder,self.root/'server.crt',self.root/'server.key',self.root/'ca.crt')
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        url='https://127.0.0.1:'+str(server.server_port)+'/objects/sample'
        try:
            with self.assertRaises(urllib.error.HTTPError) as error:
                urllib.request.urlopen(url,context=self.context())
            self.assertEqual(error.exception.code,403)
            der=ssl.PEM_cert_to_DER_cert((self.root/'client.crt').read_text())
            row['clients']=[hashlib.sha256(der).hexdigest()];entry.write_text(json.dumps(row))
            with urllib.request.urlopen(url,context=self.context()) as response:
                self.assertEqual(response.read(),source.read_bytes())
            import time
            for expiry in (time.time()-1, time.time()+3600, 'invalid', float('nan')):
                row['expires']=expiry;entry.write_text(json.dumps(row))
                with self.assertRaises(urllib.error.HTTPError) as error:
                    urllib.request.urlopen(url,context=self.context())
                self.assertEqual(error.exception.code,403)
            row['expires']=time.time()+30;entry.write_text(json.dumps(row))
            with urllib.request.urlopen(url,context=self.context()) as response:
                self.assertEqual(response.read(),source.read_bytes())
        finally:
            server.shutdown();server.server_close();thread.join()

    def test_wrong_receipt_rejected(self):
        request=urllib.request.Request(self.url,headers={'If-Match':'"'+'0'*64+'"'})
        with self.assertRaises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(request,context=self.context())
        self.assertEqual(error.exception.code,412)

    def test_connection_failure_preserves_partial(self):
        dest=self.root/'offline'/'copy'; dest.parent.mkdir()
        partial=dest.parent/'.rsync-partial'; partial.mkdir(); (partial/'copy').write_bytes(self.data[:32])
        with self.assertRaises(subprocess.CalledProcessError):
            fetch('https://127.0.0.1:1/objects/sample', dest, self.sha, len(self.data), self.root/'ca.crt', self.root/'client.crt', self.root/'client.key')
        self.assertEqual((partial/'copy').read_bytes(),self.data[:32])
        self.assertFalse(dest.exists())

    def test_ranges(self):
        self.assertEqual(byte_range('bytes=5-', 10), (5,9,True))
        for value in ['bytes=-4','bytes=3-99','bytes=10-','bytes=1-2,4-5']:
            with self.assertRaises(ValueError):byte_range(value,10)
