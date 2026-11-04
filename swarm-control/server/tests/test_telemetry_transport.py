"""Exercise dashboard mutations without constructing the live coordinator."""
import ast
import io
import json
from pathlib import Path
import unittest
from unittest.mock import Mock
from urllib.parse import urlsplit


class TransportEndpointTests(unittest.TestCase):
    def invoke(self, options, authorized=True, origin='http://map.local'):
        tree=ast.parse((Path(__file__).resolve().parents[1]/'server.py').read_text())
        handler=next(node for node in tree.body if isinstance(node,ast.ClassDef) and node.name=='Handler')
        method=next(node for node in handler.body if isinstance(node,ast.FunctionDef) and node.name=='do_POST')
        monitor=Mock();monitor.set_telemetry_transport.return_value={'transport':'webrtc','updated_at':10}
        auth=Mock(return_value=authorized)
        namespace={'urlsplit':urlsplit,'json':json,'handle_master':lambda *args:False,
                   'master_api':None,'authorize_dashboard':auth,'cpu_monitor':monitor}
        exec(compile(ast.Module(body=[method],type_ignores=[]),'server.py','exec'),namespace)
        body=json.dumps(options).encode()
        request=Mock(path='/api/telemetry/transport',rfile=io.BytesIO(body),wfile=io.BytesIO())
        request.headers={'Origin':origin,'Host':'map.local','Content-Length':str(len(body))}
        namespace['do_POST'](request)
        return request,monitor,auth

    def test_authorized_change_returns_global_policy(self):
        request,monitor,auth=self.invoke({'transport':'webrtc'})
        auth.assert_called_once_with(request,local_settings=True)
        monitor.set_telemetry_transport.assert_called_once_with('webrtc')
        request.send_response.assert_called_once_with(200)
        self.assertEqual(json.loads(request.wfile.getvalue())['transport'],'webrtc')

    def test_unauthorized_and_cross_origin_requests_cannot_mutate(self):
        for kwargs in ({'authorized':False},{'origin':'http://evil.example'},{'origin':None}):
            with self.subTest(kwargs=kwargs):
                request,monitor,_=self.invoke({'transport':'tcp'},**kwargs)
                monitor.set_telemetry_transport.assert_not_called()

    def test_malformed_policy_is_rejected(self):
        for body in ({'extra':True},['tcp'],{'transport':'tcp','extra':True}):
            request,monitor,_=self.invoke(body)
            request.send_error.assert_called_once()
            self.assertEqual(request.send_error.call_args.args[0],400)
            monitor.set_telemetry_transport.assert_not_called()
