"""Local mTLS fixture shared by generic node execution tests."""
import hashlib
import ssl
import subprocess
import threading
import pytest
from scripts.swarm_tls_transfer import make_server

@pytest.fixture(scope='module')
def tls_server(tmp_path_factory):
    root=tmp_path_factory.mktemp('range-tls')
    def openssl(*args):subprocess.run(['openssl',*args],cwd=root,check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    openssl('req','-x509','-newkey','ec','-pkeyopt','ec_paramgen_curve:P-256','-nodes','-keyout','ca.key','-out','ca.crt','-subj','/CN=ca','-days','1')
    for name,purpose in [('server','serverAuth'),('identity','clientAuth')]:
        openssl('req','-new','-newkey','ec','-pkeyopt','ec_paramgen_curve:P-256','-nodes','-keyout',name+'.key','-out',name+'.csr','-subj','/CN='+name)
        (root/(name+'.ext')).write_text('extendedKeyUsage='+purpose+'\nsubjectAltName=IP:127.0.0.1\n')
        openssl('x509','-req','-in',name+'.csr','-CA','ca.crt','-CAkey','ca.key','-CAcreateserial','-out',name+'.crt','-days','1','-extfile',name+'.ext')
    manifest=root/'objects.json';manifest.write_text('{}')
    server=make_server('127.0.0.1',0,manifest,root/'server.crt',root/'server.key',root/'ca.crt')
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    peer={'address':'127.0.0.1','certificate_sha256':hashlib.sha256(ssl.PEM_cert_to_DER_cert((root/'server.crt').read_text())).hexdigest(),'machine':'source'}
    yield root,server,peer
    server.shutdown();server.server_close();thread.join()
