import json
import subprocess

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from scripts import deploy_swarm_bulk as deploy


def test_bulk_deployment_uses_attested_mdns_and_dns_certificates(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path))
    (tmp_path/'.config/map-master').mkdir(parents=True)
    config = tmp_path/'peers.json'
    config.write_text(json.dumps({'name':'owner.local', 'controller_public_key':'a'*64,
                                  'peers':{'owner.local':'10.42.0.1', 'worker.local':'10.42.0.137',
                                           'offline.local':'10.42.0.49'}}))
    monkeypatch.setattr(deploy, 'CONFIG', config)
    def resolve(name):
        if name == 'offline.local':
            raise OSError('No mDNS endpoint')
        return 'http://'+name.removesuffix('.local')+'-swarm.local:8790', ['10.42.0.222']
    monkeypatch.setattr(deploy, 'resolve_endpoint', resolve)
    class Client:
        def __init__(self, host): self.host = host
        def call(self, *args, **kwargs):
            return {'node':self.host['host'], 'machine':'signed-'+self.host['host'],
                    'addresses':['10.42.0.222']}
        def close(self): pass
    monkeypatch.setattr(deploy, 'AgentClient', Client)
    monkeypatch.setattr(deploy.LauncherUpdates, 'command', lambda self, host, code: ['enrolled', host['host']])
    key = ec.generate_private_key(ec.SECP256R1())
    csr = x509.CertificateSigningRequestBuilder().subject_name(x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, 'test')])).sign(key, hashes.SHA256())
    installed = []
    def run(command, *, input, **kwargs):
        payload = json.loads(input)
        if 'config' not in payload:
            result = {'root':str(tmp_path/'bulk'), 'machine':payload['machine'],
                      'csr':csr.public_bytes(serialization.Encoding.PEM).decode()}
        else:
            installed.append(payload)
            result = {'node':payload['config']['name'], 'active':True}
        return subprocess.CompletedProcess(command, 0, json.dumps(result), '')
    monkeypatch.setattr(deploy.subprocess, 'run', run)
    first = deploy.deploy()
    first_certs = {p['config']['name']:p['cert'] for p in installed}
    assert len(first['installed']) == 2
    assert first['pending'] == {'offline.local':'No mDNS endpoint'}
    for payload in installed:
        name = payload['config']['name'].removesuffix('.local')+'-swarm.local'
        assert payload['config']['address'] == name
        assert payload['config']['bind'] == '0.0.0.0'
        cert = x509.load_pem_x509_certificate(payload['cert'].encode())
        san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        assert san.get_values_for_type(x509.DNSName) == [name]
        assert not san.get_values_for_type(x509.IPAddress)
    installed.clear()
    deploy.deploy()
    assert {p['config']['name']:p['cert'] for p in installed} == first_certs
