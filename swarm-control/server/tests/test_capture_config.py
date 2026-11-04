import json
import unittest
from unittest.mock import patch
from capture_config import configuration


class ConfigTests(unittest.TestCase):
    def test_host_addresses_and_routed_global_ipv6_prefixes_are_local(self):
        addresses=[{'ifname':'lan','link_type':'ether','address':'aa:bb:cc:dd:ee:ff','addr_info':[{'local':'10.42.0.1','prefixlen':24}]},
                   {'ifname':'wan','link_type':'ether','address':'aa:bb:cc:dd:ee:00','addr_info':[{'local':'2601:152:300:c16e::1','prefixlen':64}]}]
        v4=[{'dst':'default','gateway':'172.20.20.1','dev':'wan'},{'dst':'10.42.0.0/24','dev':'lan'}]
        v6=[{'dst':'2601:152:300:c100::/56','dev':'lan'},{'dst':'default','gateway':'fe80::1','dev':'wan'}]
        with patch('capture_config.subprocess.check_output', side_effect=[json.dumps(x) for x in (addresses,v4,v6)]):
            result=configuration('lan')
        self.assertEqual(set(result['interfaces']),{'lan','wan'})
        self.assertIn('2601:152:300:c16e::/64',result['local_networks'])
        self.assertIn('2601:152:300:c100::/56',result['local_networks'])
        self.assertIn('2601:152:300:c100::/56',result['interfaces']['lan']['networks'])
        self.assertIn('2601:152:300:c16e::1',result['host_addresses'])
        self.assertNotIn('::/0',result['local_networks'])


if __name__ == '__main__': unittest.main()
