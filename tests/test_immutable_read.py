"""Actual enrolled mTLS ranges and shared-page cache behavior."""
import datetime
import errno
import hashlib
import json
import socket
import ssl
import threading
from pathlib import Path
from unittest.mock import Mock
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes,serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID,ExtendedKeyUsageOID
from scripts.swarm_immutable_read import Reader,IntegrityError
from scripts.swarm_tls_transfer import make_server,identity


@pytest.fixture
def transport(tmp_path,monkeypatch):
    from scripts import swarm_immutable_read as module
    now=datetime.datetime.now(datetime.timezone.utc);ca_key=ec.generate_private_key(ec.SECP256R1());issuer=x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,'fixture-ca')])
    ca=(x509.CertificateBuilder().subject_name(issuer).issuer_name(issuer).public_key(ca_key.public_key()).serial_number(1).not_valid_before(now-datetime.timedelta(minutes=1)).not_valid_after(now+datetime.timedelta(days=1)).add_extension(x509.BasicConstraints(ca=True,path_length=0),critical=True).sign(ca_key,hashes.SHA256()))
    (tmp_path/'ca.crt').write_bytes(ca.public_bytes(serialization.Encoding.PEM))
    certs={}
    for name,purpose in [('origin',ExtendedKeyUsageOID.SERVER_AUTH),('identity',ExtendedKeyUsageOID.CLIENT_AUTH)]:
        key=ec.generate_private_key(ec.SECP256R1());subject=x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,name+'.local')])
        cert=(x509.CertificateBuilder().subject_name(subject).issuer_name(issuer).public_key(key.public_key()).serial_number(2 if name=='origin' else 3).not_valid_before(now-datetime.timedelta(minutes=1)).not_valid_after(now+datetime.timedelta(days=1)).add_extension(x509.SubjectAlternativeName([x509.DNSName(name+'.local')]),critical=False).add_extension(x509.ExtendedKeyUsage([purpose]),critical=False).sign(ca_key,hashes.SHA256()))
        (tmp_path/(name+'.crt')).write_bytes(cert.public_bytes(serialization.Encoding.PEM));(tmp_path/(name+'.key')).write_bytes(key.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,serialization.NoEncryption()));certs[name]=hashlib.sha256(cert.public_bytes(serialization.Encoding.DER)).hexdigest()
    data=bytes(range(256))*1024;source=tmp_path/'source';source.write_bytes(data);sha=hashlib.sha256(data).hexdigest();obj='a'*64
    objects=tmp_path/'objects';objects.mkdir();(objects/(obj+'.json')).write_text(json.dumps({'path':str(source),'sha256':sha,'identity':identity(source.stat()),'clients':[certs['identity']]}))
    server=make_server('127.0.0.1',0,objects,tmp_path/'origin.crt',tmp_path/'origin.key',tmp_path/'ca.crt');thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    monkeypatch.setattr(module,'connect',lambda *args,**kwargs:socket.create_connection(('127.0.0.1',server.server_port),timeout=3))
    config={'name':'target.local','machine':hashlib.sha256(Path('/etc/machine-id').read_bytes()).hexdigest(),'port':server.server_port,'peers':{'origin.local':{'address':'origin.local','machine':'b'*64,'certificate_sha256':certs['origin']}}}
    (tmp_path/'config.json').write_text(json.dumps(config))
    definition={'source':'origin.local','machine':'b'*64,'bytes':len(data),'sha256':sha,'clients':{'target.local':obj}}
    try:yield tmp_path,source,data,definition,config
    finally:server.shutdown();server.server_close();thread.join(timeout=3)


def test_pinned_range_and_changed_source_are_verified(transport):
    root,source,data,definition,config=transport;reader=Reader({'/reference':definition},root)
    assert reader.read('/reference',4096,16384)==data[4096:20480]
    assert reader.read('/reference',0,128*1024)==data[:128*1024]
    source.write_bytes(data+b'x')
    with pytest.raises(IntegrityError):reader.read('/reference',0,4096)


def test_wrong_certificate_and_wrong_etag_fail_closed(transport):
    root,source,data,definition,config=transport
    config['peers']['origin.local']['certificate_sha256']='0'*64;(root/'config.json').write_text(json.dumps(config))
    with pytest.raises(IntegrityError):Reader({'/reference':definition},root).read('/reference',0,4096)
    definition['sha256']='f'*64
    # Restore the exact pinned certificate, then reject mismatched object identity.
    cert=x509.load_pem_x509_certificate((root/'origin.crt').read_bytes());config['peers']['origin.local']['certificate_sha256']=hashlib.sha256(cert.public_bytes(serialization.Encoding.DER)).hexdigest();(root/'config.json').write_text(json.dumps(config))
    with pytest.raises(IntegrityError):Reader({'/reference':definition},root).read('/reference',0,4096)


def test_range_bound_prevents_accidental_full_reference_copy(transport):
    root,source,data,definition,config=transport;reader=Reader({'/reference':definition},root)
    with pytest.raises(IntegrityError):reader.read('/reference',0,256*1024+1)
    with pytest.raises(IntegrityError):reader.read('/reference',len(data),1)


def test_kernel_readahead_coalesces_into_existing_shared_page_keys(tmp_path,monkeypatch):
    from scripts import swarm_immutable_read as module
    from scripts.swarm_fs_mount import Client
    from scripts.run_input_cache import InputCache
    data=bytes(range(256))*512;reader=Mock();reader.read.side_effect=lambda path,start,length:data[start:start+length]
    monkeypatch.setattr(module,'Reader',lambda *args:reader)
    settings={'namespace':'test','origin':'origin.local','peers':[],'immutable_files':{'/reference':'version'},'immutable_bulk_files':{'/reference':{}}}
    client=Client(settings,cache=InputCache(tmp_path/'ram',budget=1024**2,reserve=0));client.origin=Mock(return_value={'st_size':len(data),'version':'version','st_mode':0o100444})
    assert client.read('/reference',128*1024,0)==data
    reader.read.assert_called_once_with('/reference',0,128*1024)
    assert client.read('/reference',4096,8192)==data[8192:12288]
    assert reader.read.call_count==1
    client.publish_queue.join()


def test_cold_peer_miss_is_negative_cached_and_publish_does_not_block(tmp_path,monkeypatch):
    from scripts import swarm_immutable_read as module
    from scripts.swarm_fs_mount import Client
    from scripts.run_input_cache import InputCache
    data=b'x'*(32*1024);reader=Mock();reader.read.side_effect=lambda path,start,length:data[start:start+length];monkeypatch.setattr(module,'Reader',lambda *args:reader)
    client=Client({'namespace':'test','origin':'origin.local','peers':['peer.local'],'immutable_files':{'/reference':'v'},'immutable_bulk_files':{'/reference':{}}},cache=InputCache(tmp_path/'ram',budget=1024**2,reserve=0))
    client.origin=Mock(return_value={'st_size':len(data),'version':'v','st_mode':0o100444});calls=[]
    def call(peer,operation,**args):calls.append(operation);return {'data':None} if operation=='cache.get' else {'stored':True}
    client.call=call
    assert client.read('/reference',4096,0)==data[:4096]
    assert client.read('/reference',4096,16384)==data[16384:20480]
    client.publish_queue.join();assert calls.count('cache.get')==1;assert calls.count('cache.put')==2


def test_reconnect_checks_certificate_pin_again(transport):
    root,source,data,definition,config=transport;reader=Reader({'/reference':definition},root)
    assert reader.read('/reference',0,4096)==data[:4096]
    reader.local.connections['origin.local'][0].close()
    config['peers']['origin.local']['certificate_sha256']='0'*64
    (root/'config.json').write_text(json.dumps(config))
    with pytest.raises(IntegrityError):reader.read('/reference',0,4096)
