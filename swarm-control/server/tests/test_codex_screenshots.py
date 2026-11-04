import base64
import unittest
from unittest.mock import patch,MagicMock
import codex

PNG=base64.b64encode(b'\x89PNG\r\n\x1a\n'+b'\0'*64).decode()
IMAGE={'name':'screenshot.png','data_url':'data:image/png;base64,'+PNG}
class ScreenshotTests(unittest.TestCase):
    def test_only_bounded_inline_images_are_accepted(self):
        self.assertEqual(codex.screenshot_inputs([IMAGE]),[{'type':'image','url':IMAGE['data_url']}])
        for images in [None,[IMAGE]*5,[{'name':'x','data_url':'https://example.com/a.png'}],[{'name':'x','data_url':'file:///etc/passwd'}],[{'name':'x','data_url':'data:image/svg+xml;base64,'+PNG}],[{'name':'x','data_url':'data:image/png;base64,aGVsbG8='}],[{**IMAGE,'path':'/private'}]]:
            with self.subTest(images=str(images)[:80]),self.assertRaises(codex.ContextError):codex.screenshot_inputs(images)
        large={**IMAGE,'data_url':'data:image/png;base64,'+base64.b64encode(b'\x89PNG\r\n\x1a\n'+b'0'*(2*1024*1024)).decode()}
        with self.assertRaises(codex.ContextError):codex.screenshot_inputs([large])
    def test_images_reach_send_and_steer_without_duplicate_delivery(self):
        for action in ['send','steer']:
            codex.RECEIPTS.clear()
            client=MagicMock();client.__enter__.return_value=client;client.owner.return_value='owner';client.request.return_value={'result':{}}
            with patch.object(codex,'thread',return_value={'id':'fixture-thread'}),patch.object(codex,'history',return_value={'activity':'idle' if action=='send' else 'running'}),patch.object(codex,'IPC',return_value=client):
                command={'thread_id':'fixture-thread','action':action,'text':'','images':[IMAGE],'request_id':'11111111-1111-4111-8111-111111111111'}
                self.assertTrue(codex.mutate(command)['accepted']);codex.mutate(command);client.request.assert_called_once()
                params=client.request.call_args.args[1];inputs=params['turnStart']['request']['input'] if action=='send' else params['input']
                self.assertEqual(inputs[1],{'type':'image','url':IMAGE['data_url']});self.assertIn('attached screenshots',inputs[0]['text'])
    def test_invalid_attachment_never_dispatches(self):
        with patch.object(codex,'IPC') as ipc,self.assertRaises(codex.ContextError):
            codex.mutate({'thread_id':'fixture','action':'send','text':'x','request_id':'11111111-1111-4111-8111-111111111111','images':[{'name':'x','data_url':'file:///private'}]})
        ipc.assert_not_called()
