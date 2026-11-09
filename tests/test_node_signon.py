import base64,hashlib,json
from pathlib import Path
from unittest.mock import patch
import pytest
from scripts import node_signon


def test_pkce_browser_approval_only_delivers_to_native_client(tmp_path):
    requests=[];path=tmp_path/'peers.json';invitation={'url':'https://signal.example','token':'private'}
    def post(origin,route,body):
        requests.append((route,body))
        if route.endswith('/start'):
            assert body['code_challenge_method']=='S256' and 'code_verifier' not in body
            return {'request_id':'r'*43,'authorization_url':origin+'/join-node?request='+'r'*43,'expires_in':600}
        verifier=body['code_verifier']
        assert base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode()==requests[0][1]['code_challenge']
        return {'status':'approved','config':{'name':'worker.local','peers':{'worker.local':'192.0.2.1'}},'invitation':invitation}
    with patch.object(node_signon,'post',side_effect=post),patch.object(node_signon,'local_address',return_value='192.0.2.1'),patch.object(node_signon.webbrowser,'open') as browser,patch('launcher_registration.register') as register:
        node_signon.join('https://console.example','worker.local',path)
    browser.assert_called_once_with('https://console.example/join-node?request='+'r'*43)
    assert path.stat().st_mode&0o777==0o600
    assert 'code_verifier' not in path.read_text()
    register.assert_called_once_with(path.parent/'invitation.json',path)
    assert not (path.parent/'invitation.json').exists()


def test_existing_membership_and_redirects_are_not_overwritten(tmp_path):
    path=tmp_path/'peers.json';path.write_text('{"original":true}')
    with pytest.raises(ValueError,match='already provisioned'):node_signon.join('https://console.example',config_path=path)
    assert json.loads(path.read_text())=={'original':True}
    for origin in ('http://console.example','https://user:pass@console.example','https://console.example/path'):
        with pytest.raises(ValueError):node_signon.origin_url(origin)
