import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import codex


class HistoryTests(unittest.TestCase):
    def read(self, records):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'rollout.jsonl'
            path.write_text('\n'.join(json.dumps(row) for row in records) + '\n')
            with patch.object(codex, 'HOME', Path(folder)):
                return codex.history({'rollout_path': str(path)})

    def message(self, text):
        return {'type': 'response_item', 'payload': {'type': 'message', 'role': 'assistant', 'content': [{'type': 'output_text', 'text': text}]}}

    def test_tool_output_does_not_hide_replies_or_completion(self):
        rows = [{'type': 'event_msg', 'payload': {'type': 'task_started', 'turn_id': 'turn'}}]
        rows += [self.message('First update')]
        rows += [{'type': 'response_item', 'payload': {'type': 'function_call_output', 'output': 'x' * 3000000}}]
        rows += [self.message('Final response'), {'type': 'event_msg', 'payload': {'type': 'task_complete'}}]
        result = self.read(rows)
        self.assertEqual([m['text'] for m in result['messages']], ['First update', 'Final response'])
        self.assertEqual(result['activity'], 'idle')
        self.assertFalse(result['history_limited'])

    def test_latest_sixty_messages_remain_in_order(self):
        result = self.read([self.message(str(i)) for i in range(75)])
        self.assertEqual([m['text'] for m in result['messages']], list(map(str, range(15, 75))))
        self.assertTrue(result['history_limited'])

    def test_partial_record_does_not_hide_previous_messages(self):
        result = self.read([self.message('a' * 80000), self.message('last')])
        self.assertEqual(len(result['messages']), 2)
        self.assertEqual(result['messages'][-1]['text'], 'last')
