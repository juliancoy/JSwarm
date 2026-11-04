import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
import codex as context


class ConversationTitleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        mock = patch.object(context, 'HOME', self.home)
        mock.start()
        self.addCleanup(mock.stop)
        self.ident = '11111111-1111-1111-1111-111111111111'

    def index(self, *names):
        (self.home / 'session_index.jsonl').write_text('\n'.join(
            json.dumps({'id': self.ident, 'thread_name': name}) for name in names
        ))

    def test_index_uses_latest_valid_name_and_ignores_partial_records(self):
        self.index('Old name', 'Actual Codex title', '')
        with (self.home / 'session_index.jsonl').open('a') as stream:
            stream.write('\nnull\n{"id":')
        self.assertEqual(context.indexed_titles(), {self.ident: 'Actual Codex title'})

    def test_precedence_and_missing_index(self):
        row = {'id': self.ident, 'name': 'Renamed in Codex', 'title': 'Long initial prompt'}
        self.assertEqual(context.display_title(row, {self.ident: 'Earlier index name'}), 'Renamed in Codex')
        row['name'] = None
        self.assertEqual(context.display_title(row, {self.ident: 'Actual Codex title'}), 'Actual Codex title')
        self.assertEqual(context.indexed_titles(), {})
        self.assertEqual(context.display_title(row, {}), 'Long initial prompt')
        row['title'] = ' '
        self.assertEqual(context.display_title(row, {}), 'Untitled conversation')

    def test_list_and_detail_resolve_the_same_title_without_mutating_database(self):
        self.index('Actual Codex title')
        path = self.home / 'state_5.sqlite'
        with sqlite3.connect(path) as db:
            db.execute('CREATE TABLE threads(id,title,name,cwd,updated_at,source,model,rollout_path)')
            db.execute('INSERT INTO threads VALUES(?,?,?,?,?,?,?,?)',
                       (self.ident, 'Initial prompt', None, '/fixture', 1, 'vscode', 'fixture', str(self.home / 'missing.jsonl')))
        with patch.object(context, 'IPC', side_effect=OSError('No fixture socket')):
            with patch.object(context, 'history', return_value={'messages': [], 'activity': 'idle'}):
                self.assertEqual(context.listing()['threads'][0]['display_title'], 'Actual Codex title')
                self.assertEqual(context.detail(self.ident)['display_title'], 'Actual Codex title')
        with sqlite3.connect(path) as db:
            self.assertEqual(db.execute('SELECT name,title FROM threads').fetchone(), (None, 'Initial prompt'))
