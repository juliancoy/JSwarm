import copy
import threading
import unittest
from unittest.mock import Mock

from master_api import APIError,MasterAPI
from master_mcp import dispatch


class MasterAPITests(unittest.TestCase):
    def setUp(self):
        self.run={'id':'20261024T120000Z-1234abcd','status':'running','hosts':[
            {'host':'compai.local','jobs':{'monitor':{'status':'running','logs':['one','two'],'failure_details':['failure']}}}]}
        self.tests=Mock(lock=threading.RLock())
        self.tests.view.side_effect=lambda ident=None: {'running':True,'run':copy.deepcopy(self.run),'history':[{'id':str(i)} for i in range(12)]}
        self.api=MasterAPI(self.tests)

    def test_history_is_bounded_and_details_do_not_mutate_recorded_logs(self):
        self.assertEqual(len(self.api.call('list_runs')['runs']),10)
        detail=self.api.call('get_run')['run']['hosts'][0]['jobs']['monitor']
        self.assertNotIn('logs',detail)
        self.assertEqual(self.run['hosts'][0]['jobs']['monitor']['logs'],['one','two'])
        self.assertEqual(self.api.call('get_logs',{'host':'compai.local','suite':'monitor','tail':1})['lines'],['two'])

    def test_stale_run_id_cannot_stop_the_current_run(self):
        with self.assertRaises(APIError) as result:
            self.api.call('stop_run',{'run_id':'20261024T120001Z-aaaaaaaa'})
        self.assertEqual(result.exception.status,409)
        self.tests.stop.assert_not_called()

    def test_bad_arguments_and_notifications_cannot_start_tests(self):
        status,response=dispatch(self.api,{'jsonrpc':'2.0','method':'tools/call','params':{'name':'start_run'}})
        self.assertEqual(status,400);self.tests.start.assert_not_called()
        status,response=dispatch(self.api,{'jsonrpc':'2.0','id':1,'method':'tools/call','params':{'name':'start_run','arguments':{'command':'sh'}}})
        self.assertEqual(response['error']['code'],-32602);self.tests.start.assert_not_called()

    def test_initialize_and_tools_are_discoverable(self):
        _,reply=dispatch(self.api,{'jsonrpc':'2.0','id':1,'method':'initialize','params':{'protocolVersion':'2025-11-25'}})
        self.assertIn('tools',reply['result']['capabilities'])
        _,reply=dispatch(self.api,{'jsonrpc':'2.0','id':2,'method':'tools/list'})
        start=next(tool for tool in reply['result']['tools'] if tool['name']=='start_run')
        self.assertFalse(start['annotations']['readOnlyHint'])
        self.assertIn('COMMITS AND PUSHES',start['description'])

    def test_tool_result_is_structured_and_rest_uses_the_same_operation(self):
        _,reply=dispatch(self.api,{'jsonrpc':'2.0','id':1,'method':'tools/call','params':{'name':'get_status'}})
        code,rest=self.api.rest('GET','/api/v1/master/status')
        self.assertEqual(code,200)
        self.assertEqual(reply['result']['structuredContent'],rest)
