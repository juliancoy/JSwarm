import unittest
from geography import public_ip, location


class GeographyTests(unittest.TestCase):
    def test_private_addresses_are_not_submitted(self):
        for ip in ['10.42.0.1','172.20.20.1','192.168.0.1','127.0.0.1','fe80::1','::1','224.0.0.1',None,'invalid']:
            self.assertIsNone(public_ip(ip))
        self.assertEqual(public_ip('8.8.8.8'),'8.8.8.8')

    def test_response_validation(self):
        self.assertEqual(location({'success':True,'latitude':0,'longitude':0})['latitude'],0)
        for row in [{}, {'success':False,'latitude':0,'longitude':0}, {'success':True,'latitude':None,'longitude':0}, {'success':True,'latitude':True,'longitude':0}, {'success':True,'latitude':91,'longitude':0}]:
            with self.assertRaises(ValueError):location(row)
