import json
import time

import pytest

from scripts import swarm_data_authorization as auth


@pytest.fixture
def gate(tmp_path, monkeypatch):
    now = time.time()
    config = dict(issuer='https://identity.example', resource='https://source.example/genomics',
                  introspection_endpoint='https://identity.example/oauth/mcp/introspect',
                  secret_file=str(tmp_path/'secret'), approvals_file=str(tmp_path/'approvals'))
    args = {'peer': 'worker', 'inputs': {'reads': {'path': '/private/reads.gz', 'sha256': 'a'*64, 'bytes': 10}}}
    job = 'b'*64
    approval = dict(subject='owner:alice', peer='worker', machine='machine-1', expires=now+300,
                    requests=[auth.request_digest('/v1/offers', args)])
    claims = dict(active=True, iss=config['issuer'], aud=config['resource'], sub='owner:alice',
                  scope='genomics:data', exp=now+300)
    (tmp_path/'approvals').write_text(json.dumps({job: approval}))
    (tmp_path/'approvals').chmod(0o600)
    monkeypatch.setattr(auth, 'introspect', lambda cfg, token: claims)
    return auth.DataAuthorization(config, {'worker': {'machine': 'machine-1'}}), args, job, claims, approval


def test_live_revocation_and_removal_of_approval(gate):
    gate, args, job, claims, approval = gate
    result = gate.authorize('/v1/offers', args, 'token', job)
    assert result['job_key'] == job and time.time() < result['expires'] <= time.time()+60
    claims['active'] = False
    with pytest.raises(auth.AccessDenied): gate.authorize('/v1/offers', args, 'token', job)
    claims['active'] = True
    __import__('pathlib').Path(gate.config['approvals_file']).write_text('{}')
    with pytest.raises(auth.AccessDenied): gate.authorize('/v1/offers', args, 'token', job)


@pytest.mark.parametrize('field,value', [('iss', 'https://foreign.example'), ('aud', 'other'),
    ('sub', 'owner:bob'), ('scope', 'swarm:control'), ('exp', 0), ('exp', True), ('exp', float('nan'))])
def test_invalid_identity_claims_fail_closed(gate, field, value):
    gate, args, job, claims, _ = gate
    claims[field] = value
    with pytest.raises(auth.AccessDenied): gate.authorize('/v1/offers', args, 'token', job)


def test_request_worker_and_job_are_bound(gate):
    gate, args, job, _, _ = gate
    for changed in ({**args, 'peer': 'other'}, {**args, 'inputs': {}}, {**args, 'extra': True}):
        with pytest.raises(auth.AccessDenied): gate.authorize('/v1/offers', changed, 'token', job)
    with pytest.raises(auth.AccessDenied): gate.authorize('/v1/offers', args, 'token', 'c'*64)
    gate.peers['worker']['machine'] = 'replacement'
    with pytest.raises(auth.AccessDenied): gate.authorize('/v1/offers', args, 'token', job)


def test_missing_token_and_outage_fail_closed(gate, monkeypatch):
    gate, args, job, _, _ = gate
    with pytest.raises(auth.AccessDenied): gate.authorize('/v1/offers', args, None, job)
    def unavailable(*args): raise auth.AccessDenied('Identity verification unavailable')
    monkeypatch.setattr(auth, 'introspect', unavailable)
    with pytest.raises(auth.AccessDenied): gate.authorize('/v1/offers', args, 'token', job)


def test_introspection_uses_exact_https_endpoint_and_hides_errors(tmp_path, monkeypatch):
    secret = tmp_path/'secret'; secret.write_text('s'*32);secret.chmod(0o600)
    config = dict(introspection_endpoint='https://id.example/oauth/mcp/introspect',
                  secret_file=str(secret), resource='https://source.example')
    calls = []
    class Connection:
        def __init__(self, host, port, timeout): assert host == 'id.example' and timeout == 5
        def request(self, *args, **kwargs): calls.append((args, kwargs))
        def getresponse(self): return self
        status = 200
        def read(self, limit): return b'{"active":true}'
        def close(self): pass
    monkeypatch.setattr(auth.http.client, 'HTTPSConnection', Connection)
    assert auth.introspect(config, 'private-token') == {'active': True}
    assert calls[0][1]['headers']['Authorization'] == 'Bearer '+'s'*32
    assert 'token=private-token' in calls[0][1]['body']
    Connection.status = 302
    with pytest.raises(auth.AccessDenied, match='^Identity verification unavailable$'):
        auth.introspect(config, 'private-token')
    config['introspection_endpoint'] = 'http://id.example/introspect'
    with pytest.raises(auth.AccessDenied): auth.introspect(config, 'private-token')
    assert len(calls) == 2


def test_private_authorization_files_reject_loose_permissions_and_symlinks(tmp_path):
    secret=tmp_path/'resource.secret';secret.write_text('s'*32);secret.chmod(0o644)
    with pytest.raises(ValueError,match='owner-only'):auth.read_private_file(secret)
    secret.chmod(0o600)
    assert auth.read_private_file(secret)==b's'*32
    link=tmp_path/'link';link.symlink_to(secret)
    with pytest.raises(OSError):auth.read_private_file(link)
