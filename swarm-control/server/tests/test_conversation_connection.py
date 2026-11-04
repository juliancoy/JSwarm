import unittest
from conversation_connection import ConnectionRecovery


class RecoveryTests(unittest.TestCase):
    def test_attachment_is_bounded_serial_and_never_starts_a_turn(self):
        now=[0];pending=[];opened=[]
        recovery=ConnectionRecovery(opened.append,clock=lambda:now[0],start=pending.append)
        self.assertEqual(recovery.request('a'),'connecting')
        recovery.request('a');recovery.request('b');self.assertEqual(len(pending),1)
        pending.pop(0)();self.assertEqual(opened,['a'])
        recovery.request('a');self.assertEqual(pending,[])
        recovery.request('b');pending.pop(0)();self.assertEqual(opened,['a','b'])
        for moment in [15,45]:now[0]=moment;recovery.request('a');pending.pop(0)()
        now[0]=200;self.assertEqual(recovery.request('a'),'needs_attention');self.assertEqual(opened.count('a'),3)
        recovery.request('a',retry=True);pending.pop(0)();self.assertEqual(opened.count('a'),4)
        recovery.connected('a');self.assertEqual(recovery.status('a'),'detached')

    def test_failure_does_not_block_other_conversations(self):
        pending=[]
        def fail(_):raise OSError('desktop unavailable')
        recovery=ConnectionRecovery(fail,start=pending.append)
        recovery.request('a');pending.pop(0)();recovery.request('b')
        self.assertEqual(len(pending),1)

    def test_bookkeeping_does_not_grow_from_unbounded_history(self):
        recovery=ConnectionRecovery(lambda _:None,start=lambda _:None)
        for i in range(100):recovery.request(str(i))
        self.assertEqual(recovery.request('over-limit'),'needs_attention')
        self.assertEqual(len(recovery.entries),100)

    def test_only_predispatch_connection_failure_is_safe_to_retry(self):
        import codex
        from unittest.mock import patch,MagicMock
        payload={'thread_id':'fixture','action':'send','text':'synthetic','request_id':'11111111-1111-4111-8111-111111111111'}
        client=MagicMock();client.__enter__.return_value=client
        with patch.dict(codex.RECEIPTS,{},clear=True),patch.object(codex,'thread',return_value={'id':'fixture'}),patch.object(codex,'IPC',return_value=client),patch.object(codex,'history',return_value={'activity':'idle'}):
            client.owner.side_effect=codex.ContextError('no-client-found')
            with self.assertRaises(codex.NotDispatched):codex.mutate(payload)
            self.assertNotIn(('fixture',payload['request_id']),codex.RECEIPTS)
            client.owner.side_effect=None;client.request.side_effect=OSError('response lost after dispatch')
            with self.assertRaises(OSError):codex.mutate(payload)
            self.assertTrue(codex.RECEIPTS[('fixture',payload['request_id'])]['uncertain'])
            client.request.reset_mock();self.assertTrue(codex.mutate(payload)['uncertain']);client.request.assert_not_called()
