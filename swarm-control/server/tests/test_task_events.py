import concurrent.futures
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from task_events import EventStore

class EventStoreTests(unittest.TestCase):
    def test_registration_cannot_shadow_executor_or_its_work_items(self):
        executor={'id':'r','repositories':{'repo':'pinned'},'children':[{'id':'real','status':'passed'}]}
        self.store.observe(executor,'application-tests')
        registration={'id':'r','children':[{'id':'display','status':'queued'}]}
        for category in ('registry','projection'):
            self.store.observe(registration,category)
            self.store.observe(registration,category)
        self.assertEqual(self.store.list_runs('application-tests'),[executor])
        self.assertEqual(self.store.history()['total_events'],3)
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT item_id FROM work_items').fetchall(),[('real',)])

    def test_identical_executor_payload_upgrades_legacy_category(self):
        value={'id':'r','status':'passed'}
        self.store.observe(value,'registry')
        self.store.observe(value,'application-tests')
        self.assertEqual(self.store.list_runs('application-tests'),[value])

    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.store=EventStore(Path(self.temp.name)/'events.sqlite3')
    def test_snapshot_and_event_transaction_and_reopen(self):
        self.store.observe({'id':'r','status':'running'})
        self.store.observe({'id':'r','status':'running'})
        self.store.observe({'id':'r','status':'passed'})
        reopened=EventStore(self.store.path)
        self.assertEqual(reopened.history()['total_events'],2)
        self.assertEqual(reopened.list_runs()[0]['status'],'passed')
        with reopened.connect() as db:
            self.assertEqual(db.execute('PRAGMA integrity_check').fetchone()[0],'ok')
            self.assertEqual(db.execute('PRAGMA synchronous').fetchone()[0],2)
    def test_gap_rolls_back_events_and_cursor_together(self):
        with self.assertRaises(ValueError):self.store.ingest_lines('peer','r',[(1,{'kind':'start'}),(3,{'kind':'end'})])
        self.assertEqual(self.store.history()['total_events'],0)
        self.assertEqual(self.store.cursor('peer'),0)
        self.store.ingest_lines('peer','r',[(1,{'kind':'start'}),(2,{'kind':'end'})])
        self.store.ingest_lines('peer','r',[(1,{'kind':'start'}),(2,{'kind':'end'})])
        self.assertEqual(self.store.history()['total_events'],2)
    def test_worker_replay_is_idempotent_and_repeated_events_retained(self):
        for seq in [1,1,2]:
            self.store.append('r','peer','log',{'line':'same','_worker_sequence':seq,'_worker_job':'job'})
        self.assertEqual(self.store.history()['total_events'],2)
    def test_history_is_append_only(self):
        self.store.append('r','p','started',{})
        for sql in ['DELETE FROM events','UPDATE events SET kind="changed"']:
            with self.assertRaises(sqlite3.IntegrityError):
                with self.store.connect() as db:db.execute(sql)
    def test_concurrent_workers_and_filter_cursor(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
            list(pool.map(lambda i:self.store.append('r' if i%2 else 'other','worker','log',{'i':i}),range(60)))
        rows=self.store.history('r',limit=10)
        self.assertEqual(rows['total_events'],30)
        self.assertEqual(len(rows['events']),10)
        more=self.store.history('r',after=rows['cursor'])
        self.assertEqual(len(more['events']),20)

    def test_nested_work_items_share_snapshot_transaction(self):
        self.store.observe({'id':'comparison','children':[{'id':'stage','name':'Matching','status':'running','children':[{'id':'chr1','name':'Chromosome 1','status':'passed','worker':'peer'}]}]})
        with self.store.connect() as db:
            rows=db.execute('SELECT item_id,parent_id,name,status,worker FROM work_items ORDER BY item_id').fetchall()
        self.assertEqual(rows[1],('stage/chr1','stage','Chromosome 1','passed','peer'))

    def test_worker_outbox_reconciles_after_coordinator_disconnect(self):
        self.store.observe({'id':'r','hosts':[{'host':'peer','agent_job_id':'j','jobs':{}}]})
        event={'kind':'suite','status':'passed','_worker_sequence':1,'_worker_job':'j'}
        self.store.append('r','peer','suite',event)
        self.store.ingest_worker('outbox','peer',[(1,'j',1,json.dumps({'kind':'suite','status':'passed'}),10)])
        self.assertEqual(self.store.history('r')['total_events'],2)
        self.assertEqual(self.store.cursor('outbox'),1)
        with self.assertRaises(ValueError):
            self.store.ingest_worker('outbox','peer',[(3,'j',2,'{}',11)])
        self.assertEqual(self.store.cursor('outbox'),1)

    def test_batch_replay_matches_individual_worker_event_identity(self):
        values=[{'line':str(i),'_worker_sequence':i,'_worker_job':'job'} for i in range(1,6)]
        self.store.append('r','peer','log',values[0])
        rows=[('r','peer','log',v,None) for v in values]
        self.store.append_batch(rows);self.store.append_batch(rows)
        self.assertEqual(self.store.history('r')['total_events'],5)
        self.assertEqual([e['payload']['line'] for e in self.store.history('r')['events']],['1','2','3','4','5'])
