import http.client
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch,Mock
from http.server import ThreadingHTTPServer
from api import Handler,LocalPairingHandler
import devices
import codex

class RemoteBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        p=Path(self.temp.name)/'token';p.write_text('owner'*16)
        env=patch.dict(os.environ,{'MAP_CODEX_TOKEN_FILE':str(p)});env.start();self.addCleanup(env.stop)
        device_path=patch.object(devices,'FILE',Path(self.temp.name)/'devices.json');device_path.start();self.addCleanup(device_path.stop)
        with devices.state() as state:self.cookie='__Host-compai_device='+devices.new_session(state,pidp_identity='owner-person',pidp_subject='test-owner')
        policy=patch('identity.config',return_value={'owner_canonical_user_id':'owner-person','allowed_subjects':['test-owner']});policy.start();self.addCleanup(policy.stop)
        self.server=ThreadingHTTPServer(('127.0.0.1',0),Handler);self.server.public_origin='https://compai.example'
        threading.Thread(target=self.server.serve_forever,daemon=True).start()
        self.addCleanup(self.server.server_close);self.addCleanup(self.server.shutdown)
    def request(self,path,method='GET',token=None,origin=None,host='compai.example',body=None,cookie=None):
        c=http.client.HTTPConnection(*self.server.server_address);headers={'Host':host}
        if cookie:headers['Cookie']=cookie
        if token:headers['Authorization']='Bearer '+token
        if origin:headers['Origin']=origin
        if body is not None:headers['Content-Type']='application/json';body=json.dumps(body)
        c.request(method,path,body=body,headers=headers);r=c.getresponse();status=r.status;self.last_cookie=r.getheader('Set-Cookie');r.read();c.close();return status
    def test_genome_difference_module_is_a_public_asset(self):
        self.assertEqual(self.request('/genome-comparison.js'),200)

    def test_wgs_progress_requires_owner(self):
        with patch('wgs_progress.snapshot',return_value={'available':True,'samples':[]}) as read:
            self.assertEqual(self.request('/api/v1/swarm/wgs'),401)
            read.assert_not_called()
            self.assertEqual(self.request('/api/v1/swarm/wgs',cookie=self.cookie),200)
            read.assert_called_once()

    def test_connection_feed_requires_owner_and_is_read_only(self):
        with patch('status.connections',return_value={'hosts':[]}) as read:
            self.assertEqual(self.request('/api/v1/swarm/connections'),401)
            read.assert_not_called()
            self.assertEqual(self.request('/api/v1/swarm/connections',cookie=self.cookie),200)
            read.assert_called_once()
            self.assertEqual(self.request('/api/v1/swarm/connections','POST',origin='https://compai.example',body={},cookie=self.cookie),404)

    def test_private_data_and_commands_require_owner_credentials(self):
        with patch('api.codex.rest') as rest:
            for path in ['/api/v1/master/codex/threads','/api/v1/master/codex/commands','/api/v1/master/codex/queue','/api/v1/master/codex/transcribe']:
                self.assertEqual(self.request(path,'POST' if path.endswith(('commands','transcribe')) else 'GET'),401)
            self.assertEqual(self.request('/api/v1/master/status',token='owner'*16),404)
            self.assertEqual(self.request('/../../.env',token='owner'*16),404)
            rest.assert_not_called()
    def test_screenshot_body_limit_only_applies_to_authenticated_commands(self):
        body={'images':[{'name':'screen.png','data_url':'x'*100000}]}
        with patch('api.codex.rest',return_value=(202,{'accepted':True})) as rest:
            path='/api/v1/master/codex/commands'
            self.assertEqual(self.request(path,'POST',origin='https://compai.example',body=body),401)
            self.assertEqual(self.request(path,'POST',origin='https://evil.example',body=body,cookie=self.cookie),403)
            rest.assert_not_called()
            self.assertEqual(self.request(path,'POST',origin='https://compai.example',body=body,cookie=self.cookie),202)
            self.assertEqual(self.request('/api/v1/swarm/research/jobs','POST',origin='https://compai.example',body=body,cookie=self.cookie),400)

    def test_authenticated_command_is_bound_to_public_origin(self):
        with patch('api.codex.rest',return_value=(202,{'accepted':True})) as rest:
            path='/api/v1/master/codex/commands'
            for origin in [None,'https://evil.example']:
                self.assertEqual(self.request(path,'POST',origin=origin,body={'text':'x'},cookie=self.cookie),403)
            rest.assert_not_called()
            self.assertEqual(self.request(path,'POST',origin='https://compai.example',body={'text':'x'},cookie=self.cookie),202)
            rest.assert_called_once()
    def test_apk_download_requires_pidp_and_wan_webrtc_is_retired(self):
        for name in ['status','download']:
            self.assertEqual(self.request('/api/v1/test-app/'+name),401)
        for cookie in [None,self.cookie]:
            self.assertEqual(self.request('/api/v1/test-app/offer','POST',origin='https://compai.example',body={},cookie=cookie),404)
        with patch('apk_delivery.artifact',return_value=(b'fixture-apk',{'sha256':'fixture'})):
            self.assertEqual(self.request('/api/v1/test-app/status',cookie=self.cookie),200)
            self.assertEqual(self.request('/api/v1/test-app/download',cookie=self.cookie),200)

    def test_research_actions_require_owner_and_same_origin(self):
        path='/api/v1/swarm/research/jobs'
        with patch('status.research_jobs',return_value={'profiles':[],'jobs':[]}) as jobs:
            self.assertEqual(self.request(path),401)
            self.assertEqual(self.request(path,'POST',body={}),401)
            self.assertEqual(self.request(path,'POST',body={},cookie=self.cookie),403)
            jobs.assert_not_called()
            self.assertEqual(self.request(path,cookie=self.cookie),200)
            self.assertEqual(self.request(path,'POST',body={'profile':'baseline','request_id':'fixture'},origin='https://compai.example',cookie=self.cookie),202)

    def test_research_status_requires_paired_owner(self):
        with patch('research.snapshot',return_value={'run':None}) as read:
            self.assertEqual(self.request('/api/v1/swarm/research'),401)
            read.assert_not_called()
            self.assertEqual(self.request('/api/v1/swarm/research',cookie=self.cookie),200)
            read.assert_called_once()
        for path in ('/research','/research.html','/research.js','/research.css'):
            self.assertEqual(self.request(path),200)

    def test_rebinding_host_is_denied_and_public_page_has_no_credentials(self):
        self.assertEqual(self.request('/',host='evil.example'),403)
        self.assertEqual(self.request('/'),200)

    def test_wan_pairing_is_retired_and_legacy_sessions_are_denied(self):
        pair=devices.issue();legacy=devices.session(pair)
        self.assertEqual(self.request('/pair','POST',origin='https://compai.example',body={'token':pair}),404)
        with patch('api.codex.rest') as rest:
            self.assertEqual(self.request('/api/v1/master/codex/threads',cookie='__Host-compai_device='+legacy),401)
            rest.assert_not_called()

    def test_owner_bearer_and_session_minting_cannot_bypass_pairing(self):
        with patch('api.codex.rest') as rest:
            self.assertEqual(self.request('/api/v1/master/codex/threads',token='owner'*16),401)
            self.assertEqual(self.request('/session','POST',token='owner'*16,origin='https://compai.example',body={}),404)
            rest.assert_not_called()

    def test_pidp_keeps_both_browsers_and_logout_revokes_only_one(self):
        with devices.state() as state:replacement='__Host-compai_device='+devices.new_session(state,pidp_identity='owner-person',pidp_subject='test-owner')
        with patch('api.codex.rest',return_value=(200,{'threads':[]})):
            self.assertEqual(self.request('/api/v1/master/codex/threads',cookie=self.cookie),200)
            self.assertEqual(self.request('/api/v1/master/codex/threads',cookie=replacement),200)
        self.assertEqual(self.request('/logout','POST',origin='https://compai.example',body={},cookie=self.cookie),200)
        with patch('api.codex.rest',return_value=(200,{'threads':[]})):
            self.assertEqual(self.request('/api/v1/master/codex/threads',cookie=self.cookie),401)
            self.assertEqual(self.request('/api/v1/master/codex/threads',cookie=replacement),200)

    def test_pidp_login_and_callback_use_secure_browser_cookies(self):
        import identity
        with patch('identity.start',return_value=('https://identity.example/authorize?state=fixture','__Host-compai_login=browser; Path=/; Secure; HttpOnly; SameSite=Lax')):
            self.assertEqual(self.request('/auth/login'),303)
            self.assertIn('Secure; HttpOnly; SameSite=Lax',self.last_cookie)
        with patch('identity.complete',return_value='fixture-device-session') as complete:
            self.assertEqual(self.request('/auth/callback?state=fixture&code=fixture&iss=https%3A%2F%2Fidentity.example',cookie='__Host-compai_login=browser'),303)
            self.assertIn('__Host-compai_device=fixture-device-session',self.last_cookie)
            self.assertIn('Secure; HttpOnly; SameSite=Strict',self.last_cookie)
            self.assertEqual(complete.call_args.args[2],'browser')
        with patch('identity.complete',side_effect=ValueError('private token details')):
            self.assertEqual(self.request('/auth/callback?state=bad&code=bad'),403)

    def test_remote_test_request_requires_pairing_and_rejects_parameters(self):
        path='/api/v1/swarm/tests/request'
        self.assertEqual(self.request(path,'POST',origin='https://compai.example',body={}),401)
        with patch('status.request_tests',return_value={'id':'fixture-request','status':'queued'}) as request:
            self.assertEqual(self.request(path,'POST',origin='https://compai.example',body={'command':'shell'},cookie=self.cookie),400)
            request.assert_not_called()
            self.assertEqual(self.request(path,'POST',origin='https://compai.example',body={},cookie=self.cookie),202)
            request.assert_called_once_with()

    def test_voice_requires_session_origin_and_bounded_audio(self):
        def upload(cookie=None,origin='https://compai.example',kind='audio/wav'):
            c=http.client.HTTPConnection(*self.server.server_address)
            headers={'Host':'compai.example','Origin':origin,'Content-Type':kind}
            if cookie:headers['Cookie']=cookie
            c.request('POST','/api/v1/master/codex/transcribe',body=b'RIFF'+b'0'*60,headers=headers)
            r=c.getresponse();status=r.status;r.read();c.close();return status
        with patch('voice.transcribe',return_value={'text':'draft only'}) as speech:
            self.assertEqual(upload(),401)
            self.assertEqual(upload(self.cookie,'https://evil.example'),403)
            self.assertEqual(upload(self.cookie,kind='text/plain'),400)
            speech.assert_not_called()
            self.assertEqual(upload(self.cookie),200);speech.assert_called_once()

    def test_message_actions_across_login_scenarios(self):
        import time
        def session(subject='test-owner',expires=None):
            with devices.state() as state:
                token=devices.new_session(state,pidp_identity='owner-person',pidp_subject=subject)
                if expires is not None:
                    state['sessions'][devices.digest(token)]['expires']=expires;devices.save(state)
            return token
        expired=session(expires=time.time()-10);revoked=session();devices.revoke(revoked)
        other=session('other-login');legacy=devices.session(devices.issue());renewed=session()
        scenarios=[('signed out',None,False),('expired',expired,False),('revoked',revoked,False),('other linked login',other,False),('legacy paired',legacy,False),('signed in',self.cookie.split('=',1)[1],True),('signed in again',renewed,True)]
        for label,token,allowed in scenarios:
            for action in ['queue','queue/send-now','commands']:
                with self.subTest(login=label,action=action),patch('message_queue.manager') as manager,patch('api.codex.rest',return_value=(202,{'accepted':True})) as rest:
                    manager.return_value.submit.return_value={'accepted':True}
                    manager.return_value.send_now.return_value={'accepted':True,'status':'delivered'}
                    cookie='__Host-compai_device='+token if token else None
                    status=self.request('/api/v1/master/codex/'+action,'POST',origin='https://compai.example',body={'id':'synthetic'},cookie=cookie)
                    self.assertEqual(status,(200 if action=='queue/send-now' else 202) if allowed else 401)
                    if not allowed:manager.assert_not_called();rest.assert_not_called()

    def test_send_now_requires_pidp_session_and_same_origin(self):
        path='/api/v1/master/codex/queue/send-now'
        queue=Mock();queue.send_now.return_value={'accepted':True,'status':'delivered'}
        with patch('message_queue.manager',return_value=queue):
            self.assertEqual(self.request(path,'POST',origin='https://compai.example',body={'id':'fixture'}),401)
            self.assertEqual(self.request(path,'POST',origin='https://evil.example',body={'id':'fixture'},cookie=self.cookie),403)
            queue.send_now.assert_not_called()
            self.assertEqual(self.request(path,'POST',origin='https://compai.example',body={'id':'fixture'},cookie=self.cookie),200)
            queue.send_now.assert_called_once_with({'id':'fixture'})
            self.assertEqual(self.request(path,cookie=self.cookie),404)

    def test_queue_mutation_requires_session_and_origin(self):
        with patch('message_queue.manager') as factory:
            factory.return_value.submit.return_value={'accepted':True}
            path='/api/v1/master/codex/queue'
            self.assertEqual(self.request(path,'POST',origin='https://compai.example',body={}),401)
            self.assertEqual(self.request(path,'POST',origin='https://evil.example',body={},cookie=self.cookie),403)
            factory.assert_not_called()
            self.assertEqual(self.request(path,'POST',origin='https://compai.example',body={},cookie=self.cookie),202)

class CommandDeliveryTests(unittest.TestCase):
    def test_auth_diagnostics_never_log_exception_contents(self):
        from api import audit_auth_failure
        with patch('api.logging.warning') as log:
            audit_auth_failure(ValueError('secret-code=private-browser-token'),'callback')
            self.assertNotIn('private-browser-token',str(log.call_args))
            self.assertIn('authentication_error',str(log.call_args))
    def test_new_message_carries_target_thread_id(self):
        ident='11111111-1111-1111-1111-111111111111'
        client=Mock();client.request.return_value={'result':{}}
        with patch('codex.thread',return_value={'id':ident}),patch('codex.history',return_value={'activity':'idle'}),patch('codex.IPC') as ipc,patch.dict(codex.RECEIPTS,{},clear=True):
            ipc.return_value.__enter__.return_value=client
            result=codex.mutate({'thread_id':ident,'action':'send','text':'Connectivity check','request_id':'22222222-2222-2222-2222-222222222222'})
            self.assertTrue(result['accepted'])
            params=client.request.call_args.args[1]
            self.assertEqual(params['turnStart']['request']['threadId'],params['conversationId'])

class LocalIssuerTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        key=Path(self.temp.name)/'issuer-token';key.write_text('issuer'*16)
        p=patch.dict(os.environ,{'MAP_PAIRING_TOKEN_FILE':str(key)});p.start();self.addCleanup(p.stop)
        self.server=ThreadingHTTPServer(('127.0.0.1',0),LocalPairingHandler);self.server.public_origin='https://compai.example'
        threading.Thread(target=self.server.serve_forever,daemon=True).start()
        self.addCleanup(self.server.server_close);self.addCleanup(self.server.shutdown)
    def request(self,token=None,origin=None,host='127.0.0.1:8092',body=None):
        c=http.client.HTTPConnection(*self.server.server_address);headers={'Host':host,'Content-Type':'application/json'}
        if token:headers['Authorization']='Bearer '+token
        if origin:headers['Origin']=origin
        c.request('POST','/pairing',body=json.dumps({} if body is None else body),headers=headers)
        r=c.getresponse();status=r.status;r.read();c.close();return status
    def test_loopback_and_fleet_credentials_do_not_grant_pairing(self):
        with patch('api.pairing_payload') as issue:
            self.assertEqual(self.request(),401)
            self.assertEqual(self.request('master'*16),401)
            self.assertEqual(self.request('launcher'*16),401)
            self.assertEqual(self.request('issuer'*16,origin='http://127.0.0.1:8791'),403)
            self.assertEqual(self.request('issuer'*16,host='evil.example'),403)
            self.assertEqual(self.request('issuer'*16,body={'command':'x'}),400)
            issue.assert_not_called()
    def test_scoped_native_request_issues_code(self):
        with patch('api.pairing_payload',return_value={'url':'https://compai.example/#pair=synthetic'}) as issue:
            self.assertEqual(self.request('issuer'*16),200)
            issue.assert_called_once_with('https://compai.example')

class NewConversationTests(unittest.TestCase):
    def test_creation_is_idempotent_and_does_not_need_an_existing_thread(self):
        body={'action':'new','request_id':'33333333-3333-3333-3333-333333333333'}
        result={'accepted':True,'thread_id':'44444444-4444-4444-4444-444444444444'}
        with patch('codex.new_thread',return_value=result) as create, patch.dict(codex.RECEIPTS,{},clear=True):
            self.assertEqual(codex.mutate(body),result)
            self.assertEqual(codex.mutate(body),result)
            create.assert_called_once_with()

    def test_unknown_creation_is_never_automatically_repeated(self):
        body={'action':'new','request_id':'33333333-3333-3333-3333-333333333333'}
        with patch('codex.new_thread',side_effect=OSError('disconnected')) as create, patch.dict(codex.RECEIPTS,{},clear=True):
            with self.assertRaises(OSError):codex.mutate(body)
            self.assertTrue(codex.mutate(body)['uncertain'])
            create.assert_called_once_with()

    def test_new_chat_rejects_remote_process_settings(self):
        for body in [{'action':'new'},{'action':'new','request_id':'bad'},
                     {'action':'new','request_id':'33333333-3333-3333-3333-333333333333','text':'override'}]:
            with self.assertRaises(codex.ContextError):codex.mutate(body)

    def test_empty_durable_chat_is_idle_but_truncated_history_is_not_assumed_idle(self):
        with tempfile.TemporaryDirectory() as temp,patch.object(codex,'HOME',Path(temp)):
            path=Path(temp)/'chat.jsonl'
            path.write_text(json.dumps({'type':'session_meta','payload':{}})+'\n')
            self.assertEqual(codex.history({'rollout_path':str(path)})['activity'],'idle')
            path.write_text(json.dumps({'type':'event_msg','payload':{'type':'task_started'}})+'\n')
            self.assertEqual(codex.history({'rollout_path':str(path)})['activity'],'running')
