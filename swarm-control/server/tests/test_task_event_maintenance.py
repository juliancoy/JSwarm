import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from task_events import EventStore
from task_event_maintenance import rebuild


class RebuildTests(unittest.TestCase):
    def test_rebuild_discards_snapshots_preserves_state_and_monotonic_ids(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'events.sqlite3';store=EventStore(path)
            store.observe({'id':'r','status':'passed','children':[{'id':'child','status':'passed'}]},'registry')
            large='x'*100000
            for i in range(8):store.append('r','registry-file','snapshot',{'state':large,'revision':i})
            store.ingest_lines('peer','r',[(1,{'kind':'receipt','line':'verified'})])
            with store.connect() as db:
                before_id=db.execute('SELECT max(id) FROM events').fetchone()[0]
                before_runs=db.execute('SELECT * FROM runs').fetchall()
                before_items=db.execute('SELECT * FROM work_items').fetchall()
            result=rebuild(path)
            self.assertLess(result['after_bytes'],result['before_bytes'])
            reopened=EventStore(path)
            with reopened.connect() as db:
                self.assertEqual(db.execute('SELECT * FROM runs').fetchall(),before_runs)
                self.assertEqual(db.execute('SELECT * FROM work_items').fetchall(),before_items)
                self.assertEqual(db.execute('PRAGMA auto_vacuum').fetchone()[0],2)
                self.assertEqual(db.execute('PRAGMA integrity_check').fetchone()[0],'ok')
            self.assertEqual(reopened.cursor('peer'),1)
            events=reopened.history()['events']
            self.assertFalse(any(e['kind']=='snapshot' for e in events))
            self.assertTrue(any(e['kind']=='state-checkpoint' for e in events))
            self.assertTrue(any(e['payload'].get('line')=='verified' for e in events))
            reopened.append('r','peer','next',{})
            self.assertGreater(reopened.history()['events'][-1]['id'],before_id)
            with self.assertRaises(sqlite3.IntegrityError):
                with reopened.connect() as db:db.execute('DELETE FROM events')

    def test_rebuild_applies_budget_and_age(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'events.sqlite3';store=EventStore(path)
            with patch('task_events.time.time',return_value=1):store.append('r','peer','old',{})
            store.append('r','peer','log',{'line':'x'*500})
            store.append('r','peer','receipt',{'status':'passed'})
            result=rebuild(path,max_bytes=100)
            self.assertLessEqual(result['payload_bytes'],100)
            self.assertEqual([e['kind'] for e in EventStore(path).history()['events']],['receipt'])

    def test_failed_rebuild_leaves_original_usable(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'events.sqlite3';store=EventStore(path)
            store.observe({'id':'r','status':'passed'})
            with patch('task_event_maintenance.table_digest',side_effect=RuntimeError('verification failed')):
                with self.assertRaises(RuntimeError):rebuild(path)
            self.assertEqual(store.list_runs()[0]['status'],'passed')
            self.assertEqual(store.history()['total_events'],1)
            self.assertFalse(path.with_name(path.name+'.compact').exists())
