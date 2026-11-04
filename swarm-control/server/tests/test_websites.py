import datetime
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from websites import WebsiteMonitor, summarize


def fixture():
    return {'site':'https://codecollective.us/','observed':datetime.datetime.now(datetime.timezone.utc).isoformat(),
        'requests':[{'origin':'https://codecollective.us','type':'document','status':200,'bytes':100,'duration_ms':20,'server_ip':'1.1.1.1'},
                    {'origin':'https://example.net','type':'fetch','status':None,'bytes':None,'duration_ms':None,'blocked':'non_read_method'},
                    {'origin':'https://example.net','type':'script','status':404,'bytes':20,'duration_ms':40}],
        'initiators':[{'source':'https://codecollective.us','target':'https://example.net','basis':'CDP request script/parser initiator origin'}]}


class WebsiteTests(unittest.TestCase):
    def test_unknown_bytes_and_exclusions_are_not_successes_or_site_failures(self):
        result=summarize(fixture())
        self.assertEqual(result['requests'],3)
        self.assertEqual(result['bytes'],120)
        self.assertEqual(result['errors'],1)
        self.assertEqual(result['blocked'],1)
        node=next(n for n in result['origins'] if n['origin']=='https://example.net')
        self.assertEqual(node['unknown_bytes'],1)
        self.assertEqual(node['mean_duration_ms'],40)
        self.assertEqual(result['links'][0]['requests'],1)

    def test_persistence_selection_retention_and_site_allowlist(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'websites.sqlite3'
            monitor=WebsiteMonitor(path);monitor.save(fixture())
            selected_id=monitor.view()['history'][0]['id']
            self.assertEqual(WebsiteMonitor(path).view(selected_id)['selected']['summary']['requests'],3)
            old=fixture();old['observed']=(datetime.datetime.now(datetime.timezone.utc)-datetime.timedelta(days=31)).isoformat()
            monitor.save(old)
            self.assertEqual(len(monitor.view()['history']),1)
            self.assertIsNone(monitor.view(999)['selected'])
            bad=fixture();bad['site']='http://127.0.0.1/'
            with self.assertRaises(ValueError):monitor.save(bad)

    def test_auth_challenges_and_browser_cancellations_are_separate(self):
        run=fixture()
        run['requests']=[{'origin':'https://codecollective.us','type':'fetch','status':401,'bytes':None},
                         {'origin':'https://codecollective.us','type':'image','status':None,'bytes':None,'cancelled':True,'failure_code':'net::ERR_ABORTED'}]
        result=summarize(run)
        self.assertEqual(result['errors'],0)
        self.assertEqual(result['auth_challenges'],1)
        self.assertEqual(result['cancelled'],1)
