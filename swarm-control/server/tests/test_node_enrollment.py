import hashlib,json,secrets,time
from pathlib import Path
from unittest.mock import patch
import pytest
import node_enrollment as enrollment
import devices

@pytest.fixture
def fleet(tmp_path):
    config=tmp_path/'peers.json';key=tmp_path/'signing.key'
    config.write_text(json.dumps({'name':'master.local','master':'master.local','key':'11'*32,'controller_public_key':'22'*32,
        'peers':{'master.local':'10.0.0.1'},'internet_nodes':{'master.local':'master-peer'},
        'internet':{'url':'https://signal.example','room':'a'*64}}))
    key.write_text('test-signing-secret-that-is-long-enough');key.chmod(0o600)
    policy={'owner_canonical_user_id':'owner','owner_subject':'subject','allowed_subjects':['subject']}
    with patch.object(enrollment,'CONFIG',config),patch.object(enrollment,'SIGNING_KEY',key),patch.object(enrollment,'STORE',tmp_path/'grants.sqlite'),patch.object(devices,'FILE',tmp_path/'devices.json'),patch('identity.config',return_value=policy):
        with devices.state() as state:token=devices.new_session(state,pidp_identity='owner',pidp_subject='subject')
        yield config,token


def pending():
    verifier=secrets.token_urlsafe(48)
    request={'name':'new-node.local','machine':'f'*64,'address':'192.168.1.10','code_challenge':enrollment.b64(hashlib.sha256(verifier.encode()).digest()),'code_challenge_method':'S256'}
    grant=enrollment.start('https://console.example',request)
    return grant,{'request_id':grant['request_id'],'code_verifier':verifier}


def test_owner_approval_pkce_private_delivery_and_idempotent_retry(fleet):
    config,token=fleet;before=config.read_text();grant,exchange=pending()
    assert enrollment.exchange('https://console.example',exchange)=={'status':'authorization_pending'}
    assert config.read_text()==before
    enrollment.approve(grant['request_id'],token,'https://console.example')
    details=enrollment.details(grant['request_id'])
    assert not {'key','invitation','token','code_verifier','code_challenge'}&details.keys()
    with pytest.raises(PermissionError):enrollment.exchange('https://console.example',{**exchange,'code_verifier':'x'*64})
    result=enrollment.exchange('https://console.example',exchange)
    assert result['status']=='approved' and result['config']['name']=='new-node.local'
    assert 'master_api' not in result['config']
    assert 'new-node.local' in json.loads(config.read_text())['wan_only']
    assert enrollment.exchange('https://console.example',exchange)==result
    assert enrollment.STORE.stat().st_mode&0o777==0o600
    # Recover when provisioning committed but the delivery transaction did not.
    with enrollment.database() as db:db.execute('UPDATE grants SET delivery=NULL')
    assert enrollment.exchange('https://console.example',exchange)==result


def test_revocation_and_expiry_stop_approved_grants(fleet):
    _,token=fleet;grant,exchange=pending();enrollment.approve(grant['request_id'],token,'https://console.example')
    devices.revoke(token)
    with pytest.raises(PermissionError):enrollment.exchange('https://console.example',exchange)
    with enrollment.database() as db:db.execute('UPDATE grants SET expires=0')
    with pytest.raises(ValueError):enrollment.exchange('https://console.example',exchange)


def test_plain_pkce_and_name_takeover_fail(fleet):
    config,token=fleet;grant,exchange=pending()
    enrollment.approve(grant['request_id'],token,'https://console.example')
    with enrollment.database() as db:
        row=json.loads(db.execute('SELECT request FROM grants').fetchone()[0]);row['name']='master.local'
        db.execute('UPDATE grants SET request=?',(json.dumps(row),))
    with pytest.raises(ValueError,match='already enrolled'):enrollment.exchange('https://console.example',exchange)
    row['code_challenge_method']='plain'
    with pytest.raises(ValueError,match='S256'):enrollment.start('https://console.example',row)
