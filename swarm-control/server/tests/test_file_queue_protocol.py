import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[3]/'scripts'))
from launcher_agent import Agent


class FileQueueProtocolTests(unittest.TestCase):
    def test_feed_is_idempotent_catalog_only_and_rejects_closed_or_stopped_sessions(self):
        ident='20261024T120000Z-1234abcd'
        with tempfile.TemporaryDirectory() as folder:
            agent=Agent.__new__(Agent)
            agent.config=Path(folder)/'peers.json'
            (Path(folder)/'worker-policy.json').write_text('{"mode":"always"}')
            agent.root=Path(folder)
            agent.lock=threading.RLock()
            agent.jobs={ident:{'status':'running'}}
            session=agent.folder(ident);session.mkdir()
            (session/'manifest.json').write_text(json.dumps({'granular':True,'task_catalog':['monitor--abc','map--def']}))
            for _ in range(2):
                agent.dispatch('job.feed',{'id':ident,'tasks':['monitor--abc']})
            self.assertEqual([p.name for p in (session/'tasks').iterdir()],['monitor--abc'])
            with self.assertRaises(ValueError):
                agent.dispatch('job.feed',{'id':ident,'tasks':['../../outside']})
            agent.dispatch('job.feed',{'id':ident,'close':True})
            with self.assertRaises(ValueError):
                agent.dispatch('job.feed',{'id':ident,'tasks':['map--def']})
            (session/'STOP').touch()
            with self.assertRaises(ValueError):
                agent.dispatch('job.feed',{'id':ident,'close':True})


def test_lost_terminal_close_ack_can_replay_after_success_without_adding_work(tmp_path):
    import pytest
    ident='20261024T120000Z-1234abcd'
    agent=Agent.__new__(Agent);agent.root=tmp_path;agent.lock=threading.RLock()
    agent.config=tmp_path/'peers.json'
    (tmp_path/'worker-policy.json').write_text('{"mode":"always"}')
    agent.jobs={ident:{'status':'running'}}
    folder=agent.folder(ident);folder.mkdir()
    (folder/'manifest.json').write_text(json.dumps({'granular':True,'task_catalog':[]}))
    first=agent.dispatch('job.feed',{'id':ident,'tasks':[],'close':True})
    agent.jobs[ident]['status']='completed'
    assert agent.dispatch('job.feed',{'id':ident,'tasks':[],'close':True})==first
    assert [p.name for p in (folder/'tasks').iterdir()]==['CLOSED']
    with pytest.raises(ValueError):agent.dispatch('job.feed',{'id':ident,'tasks':['monitor--new']})
    (folder/'STOP').touch()
    with pytest.raises(ValueError):agent.dispatch('job.feed',{'id':ident,'tasks':[],'close':True})
