import tempfile
import unittest
from pathlib import Path
from history import DeviceHistory


class HistoryTests(unittest.TestCase):
    def test_names_unicode_addresses_and_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'history.sqlite3'
            history = DeviceHistory(path)
            history.observe([{'mac':'AA:BB:CC:DD:EE:FF','ip':'10.42.0.4','name':'小米手机'}], '2026-10-23T00:00:00Z', 'ASUS active client', True)
            history.observe([{'mac':'aa:bb:cc:dd:ee:ff','ip':'10.42.0.8','hostname':'phone.local'}], '2026-10-23T01:00:00Z', 'Ethernet discovery')
            history = DeviceHistory(path)
            history.observe([], '2026-10-23T02:00:00Z', 'Ethernet discovery')
            record, = history.view()
            self.assertEqual(record['addresses'], ['10.42.0.4','10.42.0.8'])
            self.assertEqual({n['name'] for n in record['names']}, {'小米手机','phone.local'})
            self.assertTrue(record['last_confirmed'].startswith('2026-10-23T00:'))
            self.assertTrue(record['last_observed'].startswith('2026-10-23T01:'))

    def test_older_snapshot_does_not_overwrite_latest_times_or_confirm_cached_clients(self):
        with tempfile.TemporaryDirectory() as directory:
            history = DeviceHistory(Path(directory)/'history.sqlite3')
            device = {'mac':'aa:bb:cc:dd:ee:ff', 'name':'Earlier name','ip':'offline'}
            history.observe([device], '2026-10-23T01:00:00Z', 'ASUS remembered client')
            history.observe([device], '2026-10-23T00:00:00Z', 'ASUS remembered client')
            history.observe([{'mac':'invalid'}], '2026-10-23T00:00:00Z', 'Ethernet discovery')
            record, = history.view()
            self.assertEqual(record['addresses'], [])
            self.assertIsNone(record['last_confirmed'])
            self.assertTrue(record['first_observed'].startswith('2026-10-23T00:'))
            self.assertTrue(record['last_observed'].startswith('2026-10-23T01:'))
