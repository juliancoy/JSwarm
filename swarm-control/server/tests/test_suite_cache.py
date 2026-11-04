import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from suite_cache import SuiteCache


def worker(machine='machine-a',suite='monitor'):
    return {'host':machine+'.local','session':{'machine':machine},'agent_status':{'source_revision':'agent-v1','platform':'linux/amd64'},
            'jobs':{suite:{'name':suite,'status':'queued','logs':[]}}}


def record(ident,status='passed',machine='machine-a'):
    host=worker(machine)
    host['jobs']['monitor'].update(status=status,input_fingerprint='same-inputs')
    return {'id':ident,'finished':'2026-10-24T00:00:00Z','hosts':[host]}


class SuiteCacheTests(unittest.TestCase):
    def test_split_repository_changes_invalidate_unchanged_compatibility_links(self):
        cache=SuiteCache('.','.',{'monitor':{'image':'python'}})
        image=Mock();image.fingerprint.return_value='image-v1'
        run={'repositories':{name:{'commit':'v1','url':name} for name in ('.','JSwarm','GenomeHarbor')}}
        cache.tree=lambda name,commit:[('scripts/launcher_agent.py' if name=='.' else 'owned.py',(name+commit).encode())]
        before=cache.fingerprint('monitor',worker(),run,image)
        run['repositories']['JSwarm']['commit']='v2'
        after=cache.fingerprint('monitor',worker(),run,image)
        self.assertNotEqual(before,after)
        run['repositories']['GenomeHarbor']['commit']='v2'
        self.assertNotEqual(after,cache.fingerprint('monitor',worker(),run,image))

    def test_reuses_recorded_pass_with_explicit_provenance(self):
        with tempfile.TemporaryDirectory() as folder:
            old=record('20261024T000000Z-aaaaaaaa')
            path=Path(folder)/(old['id']+'.json');path.write_text(json.dumps(old));original=path.read_bytes()
            current={'id':'20261024T000001Z-bbbbbbbb','hosts':[worker()]}
            cache=SuiteCache('.',folder,{});cache.fingerprint=Mock(return_value='same-inputs')
            cache.apply(current,Mock())
            job=current['hosts'][0]['jobs']['monitor']
            self.assertEqual(job['status'],'reused');self.assertTrue(job['not_executed'])
            self.assertEqual(job['reused_from']['run_id'],old['id'])
            self.assertEqual(path.read_bytes(),original)

    def test_newer_failure_invalidates_an_older_matching_pass(self):
        with tempfile.TemporaryDirectory() as folder:
            for ident,status in [('20261024T000000Z-aaaaaaaa','passed'),('20261024T000001Z-bbbbbbbb','failed')]:
                value=record(ident,status);(Path(folder)/(ident+'.json')).write_text(json.dumps(value))
            current={'id':'20261024T000002Z-cccccccc','hosts':[worker()]}
            cache=SuiteCache('.',folder,{});cache.fingerprint=Mock(return_value='same-inputs')
            cache.apply(current,Mock())
            self.assertEqual(current['hosts'][0]['jobs']['monitor']['status'],'queued')

    def test_different_machine_cannot_reuse_a_pass(self):
        with tempfile.TemporaryDirectory() as folder:
            old=record('20261024T000000Z-aaaaaaaa');(Path(folder)/(old['id']+'.json')).write_text(json.dumps(old))
            current={'id':'20261024T000001Z-bbbbbbbb','hosts':[worker('machine-b')]}
            cache=SuiteCache('.',folder,{});cache.fingerprint=Mock(return_value='same-inputs')
            cache.apply(current,Mock())
            self.assertEqual(current['reuse']['suites'],0)

    def test_irrelevant_commit_changes_do_not_invalidate_web_suite(self):
        cache=SuiteCache('.','.',{'web':{'image':'node','test':['npm','test']}})
        image=Mock();image.fingerprint.return_value='image-v1'
        run={'repositories':{'.':{'commit':'root','url':'root-url'},'OrgPortal':{'commit':'portal','url':'portal-url'}}}
        entries=[('web/src/App.tsx',b'web-v1'),('org-worker/src/unrelated.ts',b'api-v1')]
        cache.tree=lambda name,commit:[('swarm-control/server/distributed_worker.py',b'runner-v1')] if name=='.' else entries
        before=cache.fingerprint('web',worker(),run,image)
        entries[1]=('org-worker/src/unrelated.ts',b'api-v2')
        run['repositories']['OrgPortal']['commit']='another-commit'
        self.assertEqual(before,cache.fingerprint('web',worker(),run,image))
        entries[0]=('web/src/App.tsx',b'web-v2')
        self.assertNotEqual(before,cache.fingerprint('web',worker(),run,image))

    def test_usajobs_styles_and_test_changes_invalidate_browser_results(self):
        cache=SuiteCache('.','.',{'mobile':{'base':'usajobs','image':'browser','test':['node','--test']}})
        image=Mock();image.fingerprint.return_value='browser-v1'
        run={'repositories':{'.':{'commit':'root','url':'root-url'},'CodeCollective':{'commit':'site','url':'site-url'}}}
        entries=[('usa/index.html',b'layout-v1'),('css/master.css',b'css-v1'),('tests/usajobs-mobile.test.cjs',b'test-v1'),('README.md',b'docs-v1')]
        cache.tree=lambda name,commit:[('swarm-control/server/distributed_worker.py',b'runner-v1')] if name=='.' else entries
        before=cache.fingerprint('mobile',worker(),run,image)
        entries[3]=('README.md',b'docs-v2')
        self.assertEqual(before,cache.fingerprint('mobile',worker(),run,image))
        entries[0]=('usa/index.html',b'layout-v2')
        self.assertNotEqual(before,cache.fingerprint('mobile',worker(),run,image))
