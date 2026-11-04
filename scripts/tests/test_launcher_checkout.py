"""A pinned shallow cache with no branches must still produce a usable checkout."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from launcher_agent import Agent

class CheckoutTests(unittest.TestCase):
    def test_cached_custom_ref_materializes_without_network_or_new_branches(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            def git(path,*args):
                return subprocess.check_output(['git','-c','core.hooksPath=/dev/null','-C',str(path),*args],stderr=subprocess.PIPE,text=True).strip()
            source=root/'fixture';source.mkdir();git(source,'init','--quiet')
            (source/'fixture.txt').write_text('pinned content\n')
            for name in ('usa/index.html','css/master.css','js/reusableComponents.js','run.py','tests/usajobs-mobile.test.cjs','unrelated-media.bin'):
                target=source/name;target.parent.mkdir(parents=True,exist_ok=True);target.write_text('fixture')
            git(source,'add','.');git(source,'-c','user.name=Fixture','-c','user.email=fixture@example.test','commit','-qm','fixture')
            commit=git(source,'rev-parse','HEAD');url='https://github.com/example/fixture.git'
            jobs=root/'jobs';cache=jobs/'git-cache'/(hashlib.sha256(url.encode()).hexdigest()+'.git');cache.mkdir(parents=True)
            git(cache,'init','--bare','--quiet');git(cache,'remote','add','origin',url)
            git(cache,'fetch','--depth=1',str(source),commit+':refs/cache/'+commit)
            self.assertEqual(git(cache,'for-each-ref','--format=%(refname)','refs/heads'),'')
            config=root/'peers.json';config.write_text(json.dumps({'name':'fixture.local'}))
            agent=Agent.__new__(Agent);agent.root=jobs;agent.config=config;agent.storage_lock=threading.RLock();agent.lock=threading.RLock();agent.jobs={};agent.changed=threading.Condition(agent.lock)
            work=jobs/'run'/'source';work.mkdir(parents=True)
            manifest={'id':'run','repositories':{'.':{'url':url,'commit':commit}},'hosts':[{'host':'fixture.local','suites':['monitor','swarm-web'],'jobs':{}}]}
            job={'sequence':0,'events':[]}
            agent.checkout(work,manifest,{},job)
            self.assertEqual((work/'fixture.txt').read_text(),'pinned content\n')
            self.assertEqual(git(work,'rev-parse','HEAD'),commit)
            self.assertEqual(git(work,'status','--porcelain'),'?? distributed-plan.json')
            self.assertFalse((work/'.git/objects/info/alternates').exists())
            self.assertEqual(git(cache,'for-each-ref','--format=%(refname)','refs/heads'),'')

            # Interrupted fetches must not permanently poison the disposable cache.
            (cache/'shallow.lock').write_text('abandoned')
            recovered=jobs/'recovered'/'source';recovered.mkdir(parents=True)
            agent.checkout(recovered,{**manifest,'id':'recovered'}, {},dict(sequence=0,events=[]))
            self.assertFalse((cache/'shallow.lock').exists())
            self.assertEqual((recovered/'fixture.txt').read_text(),'pinned content\n')

            # The browser suite needs real pinned site code, not unrelated media.
            mobile=jobs/'mobile'/'source';mobile.mkdir(parents=True)
            manifest['repositories']['CodeCollective']={'url':url,'commit':commit}
            manifest['hosts'][0]['suites']=['usajobs']
            agent.checkout(mobile,{**manifest,'id':'mobile'}, {},dict(sequence=0,events=[]))
            site=mobile/'CodeCollective'
            self.assertEqual((site/'usa/index.html').read_text(),'fixture')
            self.assertTrue((site/'tests/usajobs-mobile.test.cjs').is_file())
            self.assertFalse((site/'unrelated-media.bin').exists())
            self.assertEqual(git(site,'rev-parse','HEAD'),commit)
            self.assertEqual(git(site,'status','--porcelain'),'')
