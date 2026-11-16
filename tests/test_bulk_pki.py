"""Enrolled certificates verify under strict RFC 5280 TLS policy."""
import datetime
import ssl
import threading
import socket
from cryptography import x509
from cryptography.hazmat.primitives import hashes,serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID,ExtendedKeyUsageOID
from scripts.deploy_swarm_bulk import certificate_profile,current_certificate_profile
from scripts.swarm_tls_transfer import make_server


def test_enrolled_chain_passes_strict_mutual_tls(tmp_path):
    now=datetime.datetime.now(datetime.timezone.utc);ca_key=ec.generate_private_key(ec.SECP256R1())
    subject=x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,'enrolled-ca')])
    builder=(x509.CertificateBuilder().subject_name(subject).issuer_name(subject).public_key(ca_key.public_key()).serial_number(1)
        .not_valid_before(now-datetime.timedelta(minutes=1)).not_valid_after(now+datetime.timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True,path_length=0),critical=True))
    legacy=builder.sign(ca_key,hashes.SHA256());assert not current_certificate_profile(legacy,authority=True)
    ca=certificate_profile(builder,ca_key.public_key(),ca_key.public_key(),authority=True).sign(ca_key,hashes.SHA256())
    assert current_certificate_profile(ca,authority=True)
    (tmp_path/'ca.crt').write_bytes(ca.public_bytes(serialization.Encoding.PEM))
    for name in ('server','client'):
        key=ec.generate_private_key(ec.SECP256R1());name_subject=x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,name+'.local')])
        leaf=(x509.CertificateBuilder().subject_name(name_subject).issuer_name(ca.subject).public_key(key.public_key()).serial_number(2 if name=='server' else 3)
            .not_valid_before(now-datetime.timedelta(minutes=1)).not_valid_after(now+datetime.timedelta(days=1))
            .add_extension(x509.BasicConstraints(ca=False,path_length=None),critical=True)
            .add_extension(x509.SubjectAlternativeName([x509.DNSName(name+'.local')]),critical=False)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH,ExtendedKeyUsageOID.CLIENT_AUTH]),critical=False))
        cert=certificate_profile(leaf,key.public_key(),ca_key.public_key(),authority=False).sign(ca_key,hashes.SHA256())
        assert current_certificate_profile(cert,authority=False)
        (tmp_path/(name+'.crt')).write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        (tmp_path/(name+'.key')).write_bytes(key.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,serialization.NoEncryption()))
    objects=tmp_path/'objects';objects.mkdir()
    server=make_server('127.0.0.1',0,objects,tmp_path/'server.crt',tmp_path/'server.key',tmp_path/'ca.crt')
    # Exercise both sides under the newer Python default, including on old hosts.
    server.socket.context.verify_flags |= ssl.VERIFY_X509_STRICT
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    context=ssl.create_default_context(cafile=str(tmp_path/'ca.crt'));context.verify_flags |= ssl.VERIFY_X509_STRICT
    context.load_cert_chain(tmp_path/'client.crt',tmp_path/'client.key')
    try:
        with socket.create_connection(('127.0.0.1',server.server_port),timeout=3) as raw:
            with context.wrap_socket(raw,server_hostname='server.local') as connection:
                connection.sendall(b'GET /objects/missing HTTP/1.0\r\nHost: server.local\r\n\r\n')
                assert b'404' in connection.recv(4096)
    finally:server.shutdown();server.server_close();thread.join(timeout=3)
