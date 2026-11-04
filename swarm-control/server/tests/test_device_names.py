import unittest
from device_names import retain_device_names

NOW='2026-10-26T21:00:00+00:00'


class DeviceNameTests(unittest.TestCase):
    def test_missed_reply_keeps_last_name_by_mac_without_claiming_connectivity(self):
        device={'ip':'10.42.0.50','mac':'AA:BB:CC:DD:EE:FF','hostname':None,'ping':False}
        record={'mac':'aa:bb:cc:dd:ee:ff','names':[{'name':'worker.local','source':'Ethernet discovery','last_seen':'2026-10-26T20:59:00Z'}]}
        row=retain_device_names([device],[record],NOW)[0]
        self.assertEqual(row['hostname'],'worker.local')
        self.assertEqual(row['hostname_source'],'cached_mdns')
        self.assertEqual(row['hostname_last_seen'],'2026-10-26T20:59:00Z')
        self.assertFalse(row['ping']);self.assertIsNone(device['hostname'])

    def test_ip_reuse_does_not_inherit_another_devices_name(self):
        device={'ip':'10.42.0.50','mac':'11:22:33:44:55:66','hostname':None}
        record={'mac':'aa:bb:cc:dd:ee:ff','addresses':['10.42.0.50'],'names':[{'name':'old.local','source':'Ethernet discovery','last_seen':NOW}]}
        self.assertIsNone(retain_device_names([device],[record],NOW)[0]['hostname'])

    def test_expired_or_non_mdns_names_are_not_restored(self):
        device={'mac':'aa','hostname':None}
        for seen,source in [('2026-10-24T20:59:00Z','Ethernet discovery'),(NOW,'ASUS remembered client')]:
            record={'mac':'aa','names':[{'name':'old.local','source':source,'last_seen':seen}]}
            self.assertIsNone(retain_device_names([device],[record],NOW)[0]['hostname'])

    def test_fresh_reply_wins_and_latest_cached_observation_is_selected(self):
        record={'mac':'aa','names':[{'name':'older.local','source':'Ethernet discovery','last_seen':'2026-10-26T20:00:00Z'},
                                  {'name':'newer.local','source':'Ethernet discovery','last_seen':'2026-10-26T20:30:00Z'}]}
        self.assertEqual(retain_device_names([{'mac':'aa','hostname':None}],[record],NOW)[0]['hostname'],'newer.local')
        fresh=retain_device_names([{'mac':'aa','hostname':'renamed.local'}],[record],NOW)[0]
        self.assertEqual(fresh['hostname'],'renamed.local');self.assertEqual(fresh['hostname_source'],'mdns')
