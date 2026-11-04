import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch
import codex


class FullAccessTests(unittest.TestCase):
    def test_only_explicit_full_access_disables_approval(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(codex, 'HOME', Path(directory)):
            with sqlite3.connect(Path(directory) / 'state_5.sqlite') as db:
                db.execute('CREATE TABLE threads(id, sandbox_policy)')
                policies = [{'type': 'disabled'}, {'type': 'danger-full-access'},
                            {'type': 'workspace-write'}, {'type': 'managed'}, None, 'broken']
                for i, policy in enumerate(policies):
                    db.execute('INSERT INTO threads VALUES (?,?)', (str(i), json.dumps(policy)))
            for i, policy in enumerate(policies):
                with self.subTest(policy=policy):
                    result = codex.full_access_turn_settings(str(i))
                    self.assertEqual(result, {'approvalPolicy': 'never', 'approvalsReviewer': 'user'} if i < 2 else {})
                    self.assertNotIn('sandboxPolicy', result)
            self.assertEqual(codex.full_access_turn_settings('missing'), {})

    def test_full_access_override_reaches_owner_without_replacing_other_settings(self):
        client = MagicMock()
        client.__enter__.return_value = client
        client.request.return_value = {'result': {}}
        with patch.dict(codex.RECEIPTS, {}, clear=True), patch.object(codex, 'IPC', return_value=client), \
             patch.object(codex, 'thread', return_value={'id': 'fixture'}), \
             patch.object(codex, 'history', return_value={'activity': 'idle'}), \
             patch.object(codex, 'full_access_turn_settings', return_value={'approvalPolicy': 'never', 'approvalsReviewer': 'user'}):
            codex.mutate({'thread_id': 'fixture', 'action': 'send', 'text': 'fixture',
                          'request_id': '11111111-1111-4111-8111-111111111111'})
        turn = client.request.call_args.args[1]['turnStart']
        self.assertEqual(turn['request']['approvalPolicy'], 'never')
        self.assertTrue(turn['context']['inheritThreadSettings'])
        self.assertNotIn('sandboxPolicy', turn['request'])
