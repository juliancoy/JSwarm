import base64
import hashlib
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, utils
from cryptography.exceptions import InvalidSignature
import devices as devices
import identity as auth

ORIGIN = 'https://compai.example'


def encoded(value):
    return base64.urlsafe_b64encode(value).decode().rstrip('=')


class PIdPRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)
        self.cfg = {'issuer': 'https://identity.example', 'resource': ORIGIN + '/api/swarm', 'scope': 'swarm:control',
                    'authorization_endpoint': 'https://identity.example/authorize',
                    'token_endpoint': 'https://identity.example/token', 'jwks_uri': 'https://identity.example/keys',
                    'redirect_uri': ORIGIN + '/auth/callback', 'owner_subject': 'website:app:owner',
                    'client_id': 'test-client', 'client_secret': 'test-client-secret',
                    'introspection_endpoint':'https://identity.example/introspect','resource_secret':'test-resource-'*4,
                    'owner_canonical_user_id':'owner-person'}
        config = self.path / 'config.json'; config.write_text(json.dumps(self.cfg)); config.chmod(0o600)
        for item in [patch.object(auth, 'CONFIG', config), patch.object(devices, 'FILE', self.path / 'devices.json'),
                     patch.dict(auth.PENDING, {}, clear=True)]:
            item.start(); self.addCleanup(item.stop)
        self.private = ec.generate_private_key(ec.SECP256R1())
        numbers = self.private.public_key().public_numbers()
        self.jwk = {'kid': 'test-key', 'kty': 'EC', 'crv': 'P-256',
                    'x': encoded(numbers.x.to_bytes(32, 'big')), 'y': encoded(numbers.y.to_bytes(32, 'big'))}

    def token(self, **changes):
        claims = {'iss': self.cfg['issuer'], 'aud': self.cfg['resource'], 'sub': self.cfg['owner_subject'],
                  'client_id': self.cfg['client_id'], 'scope': 'swarm:control', 'iat': time.time(), 'exp': time.time() + 300}
        claims.update(changes)
        content = encoded(json.dumps({'alg': 'ES256', 'kid': 'test-key'}).encode()) + '.' + encoded(json.dumps(claims).encode())
        r, s = utils.decode_dss_signature(self.private.sign(content.encode(), ec.ECDSA(hashes.SHA256())))
        return content + '.' + encoded(r.to_bytes(32, 'big') + s.to_bytes(32, 'big'))

    def introspection(self,subject=None,canonical='owner-person',active=True):
        return {'active':active,'sub':subject or self.cfg['owner_subject'],'iss':self.cfg['issuer'],'aud':self.cfg['resource'],'scope':self.cfg['scope'],'canonical_user_id':canonical}

    def login(self):
        url, cookie = auth.start(ORIGIN)
        query = parse_qs(urlsplit(url).query)
        return {'state': query['state'], 'code': ['one-use-code'], 'iss': [self.cfg['issuer']]}, cookie.split('=', 1)[1].split(';', 1)[0], query

    def test_pkce_and_owner_private_configuration(self):
        query, browser, request = self.login()
        pending = auth.PENDING[query['state'][0]]
        self.assertEqual(request['code_challenge'], [encoded(hashlib.sha256(pending['verifier'].encode()).digest())])
        self.assertEqual(request['code_challenge_method'], ['S256'])
        self.assertNotEqual(browser, pending['verifier'])
        self.assertEqual(request['scope'], ['swarm:control'])
        auth.CONFIG.chmod(0o644)
        with self.assertRaises(auth.Denied):auth.start(ORIGIN)

    def test_verified_owner_adds_controller_and_preserves_pairing_link(self):
        old = devices.session(devices.issue()); unused = devices.issue()
        query, browser, _ = self.login()
        token = self.token()
        with patch.object(auth, 'fetch', side_effect=[{'access_token': token}, {'keys': [self.jwk]},self.introspection()]) as fetch:
            session = auth.complete(ORIGIN, query, browser)
            exchange = parse_qs(fetch.call_args_list[0].args[1].decode())
            self.assertEqual(exchange['resource'], [self.cfg['resource']])
            self.assertIn('code_verifier', exchange)
        self.assertTrue(devices.pidp_valid(session)); self.assertTrue(devices.valid(old)); self.assertFalse(devices.pidp_valid(old))
        self.assertTrue(devices.valid(devices.session(unused)))
        stored = devices.FILE.read_text()
        self.assertNotIn(token, stored); self.assertNotIn(session, stored)
        self.assertEqual(len(json.loads(stored)['sessions']), 3)

    def test_verified_session_uses_pidp_profile_and_rejects_non_https_avatars(self):
        with devices.state() as state:token=devices.new_session(state,'Laptop',pidp_identity='owner-person',pidp_subject=self.cfg['owner_subject'])
        self.addCleanup(auth.PROFILE_CACHE.clear);auth.PROFILE_CACHE.clear()
        with patch.object(auth,'fetch',return_value=[{'id':'owner-person','display_name':'Test Owner','avatar_url':'https://identity.example/avatar.png'}]):
            profile=auth.session_info(ORIGIN,token)
        self.assertEqual(profile['user'],{'name':'Test Owner','avatar_url':'https://identity.example/avatar.png'})
        self.assertNotIn('pidp_identity',profile)
        auth.PROFILE_CACHE.clear()
        with patch.object(auth,'fetch',return_value=[{'id':'owner-person','avatar_url':'javascript:alert(1)'}]):
            self.assertIsNone(auth.session_info(ORIGIN,token)['user']['avatar_url'])
        with self.assertRaises(auth.Denied):auth.session_info(ORIGIN,devices.session(devices.issue()))

    def test_other_linked_email_identity_cannot_use_the_same_canonical_owner(self):
        query,browser,_=self.login()
        with patch.object(auth,'fetch',side_effect=[{'access_token':self.token(sub='website:app:other-email')},{'keys':[self.jwk]},self.introspection('website:app:other-email')]):
            with self.assertRaisesRegex(auth.Denied,'identity is not allowed'):auth.complete(ORIGIN,query,browser)
        with devices.state() as state:
            allowed=devices.new_session(state,pidp_identity='owner-person',pidp_subject=self.cfg['owner_subject'])
            other=devices.new_session(state,pidp_identity='owner-person',pidp_subject='website:app:other-email')
            old=devices.new_session(state,pidp_identity='owner-person')
        self.assertTrue(auth.valid_session(ORIGIN,allowed))
        self.assertFalse(auth.valid_session(ORIGIN,other))
        self.assertFalse(auth.valid_session(ORIGIN,old))
        self.cfg['allowed_subjects']=['website:app:replacement']
        auth.CONFIG.write_text(json.dumps(self.cfg));auth.CONFIG.chmod(0o600)
        self.assertFalse(auth.valid_session(ORIGIN,allowed))

    def test_other_account_cannot_disconnect_owner(self):
        old = devices.session(devices.issue()); query, browser, _ = self.login()
        with patch.object(auth, 'fetch', side_effect=[{'access_token': self.token(sub='website:app:attacker')}, {'keys': [self.jwk]},self.introspection('website:app:attacker','attacker-person')]):
            with self.assertRaises(auth.Denied):auth.complete(ORIGIN, query, browser)
        self.assertTrue(devices.valid(old))

    def test_state_is_browser_bound_single_use_and_exchange_is_not_retried(self):
        query, browser, _ = self.login()
        with patch.object(auth, 'fetch', side_effect=OSError('network failure')) as fetch:
            with self.assertRaises(auth.Denied):auth.complete(ORIGIN, query, 'wrong-browser-cookie-value')
            fetch.assert_not_called()
            with self.assertRaises(OSError):auth.complete(ORIGIN, query, browser)
            with self.assertRaises(auth.Denied):auth.complete(ORIGIN, query, browser)
            self.assertEqual(fetch.call_count, 1)

    def test_rejects_signed_tokens_with_wrong_claims_and_invalid_signature(self):
        for changes in [{'iss': 'https://evil.example'}, {'aud': 'https://elsewhere.example'},
                        {'scope': ''}, {'client_id': 'another-client'}, {'exp': time.time() - 1}, {'iat': time.time() + 120},
                        {'exp': float('nan')}, {'exp': True}]:
            with self.subTest(changes=changes), patch.object(auth, 'fetch', return_value={'keys': [self.jwk]}):
                with self.assertRaises(auth.Denied):auth.verify(self.token(**changes), self.cfg)
        token = self.token(); parts = token.split('.'); signature = bytearray(auth.decode(parts[2])); signature[0] ^= 1
        with patch.object(auth, 'fetch', return_value={'keys': [self.jwk]}):
            with self.assertRaises(InvalidSignature):auth.verify('.'.join(parts[:2] + [encoded(signature)]), self.cfg)

    def test_duplicate_or_expired_callback_never_exchanges_code(self):
        query, browser, _ = self.login()
        with patch.object(auth, 'fetch') as fetch:
            with self.assertRaises(auth.Denied):auth.complete(ORIGIN, {**query, 'code': ['a', 'b']}, browser)
            with patch.object(auth.time, 'time', return_value=time.time() + 601):
                with self.assertRaises(auth.Denied):auth.complete(ORIGIN, query, browser)
            fetch.assert_not_called()

    def test_actual_pidp_issuer_parameter_is_required_and_validated_before_exchange(self):
        query, browser, _ = self.login()
        with patch.object(auth, 'fetch') as fetch:
            for issuer in [[], ['https://evil.example'], [self.cfg['issuer'], self.cfg['issuer']]]:
                with self.subTest(issuer=issuer), self.assertRaises(auth.Denied):
                    auth.complete(ORIGIN, {**query, 'iss': issuer}, browser)
            without = {k: v for k, v in query.items() if k != 'iss'}
            with self.assertRaises(auth.Denied):auth.complete(ORIGIN, without, browser)
            fetch.assert_not_called()
        self.assertIn(query['state'][0], auth.PENDING)

    def test_reauthentication_rotates_only_current_browser(self):
        current=devices.session(devices.issue());other=devices.session(devices.issue())
        url,cookie=auth.start(ORIGIN,current,'Phone')
        state=parse_qs(urlsplit(url).query)['state']
        query={'state':state,'code':['one-use-code'],'iss':[self.cfg['issuer']]}
        browser=cookie.split('=',1)[1].split(';',1)[0]
        with patch.object(auth,'fetch',side_effect=[{'access_token':self.token()},{'keys':[self.jwk]},self.introspection()]):
            renewed=auth.complete(ORIGIN,query,browser)
        self.assertFalse(devices.valid(current));self.assertTrue(devices.valid(other));self.assertTrue(devices.valid(renewed))
        self.assertEqual(devices.describe(renewed)['label'],'Phone')

    def test_only_pidp_verified_linked_identity_can_use_the_owner_account(self):
        subject='owner:owner-person'
        self.cfg['allowed_subjects']=[subject,self.cfg['owner_subject']]
        with patch.object(auth,'fetch',side_effect=[{'keys':[self.jwk]},self.introspection(subject)]):
            auth.verify(self.token(sub=subject),self.cfg)
        with patch.object(auth,'fetch',side_effect=[{'keys':[self.jwk]},self.introspection(subject,active=False)]):
            with self.assertRaises(auth.Denied):auth.verify(self.token(sub=subject),self.cfg)
