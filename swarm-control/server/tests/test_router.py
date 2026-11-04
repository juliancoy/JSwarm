import unittest
from router import health, temperatures, netdev_counters, rates, array


class RouterTests(unittest.TestCase):
    def test_parse_health_without_evaluating_javascript(self):
        body='mem_stats_arr = ["900","500","0","20","0","0","1","2","300","600"]; cpu_stats_arr=["1.2","1","0.5"]; conn_stats_arr=["40","2"];'
        parsed=health(body)
        self.assertEqual(parsed['memory_used_mb'],300)
        self.assertEqual(parsed['load_average_1_5_15'],[1.2,1,.5])
        self.assertEqual(parsed['active_connections'],2)
        with self.assertRaises(ValueError):
            array('mem_stats_arr=[do_something()];','mem_stats_arr')
        with self.assertRaises(ValueError):
            health('mem_stats_arr=[]; cpu_stats_arr=[]; conn_stats_arr=[];')

    def test_hex_counters_deltas_reset_stale_and_missing(self):
        counters=netdev_counters({'WIRED_rx':'0x100','WIRED_tx':'512','WIRELESS0_rx':'','WIRELESS0_tx':'','secret':'ignored'})
        self.assertEqual(counters,{'WIRED':{'rx_bytes':256,'tx_bytes':512}})
        self.assertIsNone(rates(counters,{},15)['WIRED']['rx_mbps'])
        previous={'WIRED':{'rx_bytes':128,'tx_bytes':1000}}
        row=rates(counters,previous,2)['WIRED']
        self.assertEqual(row['rx_mbps'],128*8/2/1e6)
        self.assertIsNone(row['tx_mbps'])
        self.assertIsNone(rates(counters,previous,61)['WIRED']['rx_mbps'])

    def test_temperatures_only_whitelisted_numeric_fields(self):
        body='curr_cpuTemp="69.930"; curr_coreTmp_wl0_raw="45&deg;C"; curr_coreTmp_wl1_raw="<i>disabled</i>";'
        self.assertEqual(temperatures(body),{'cpu_celsius':69.93,'radio_0_celsius':45})


if __name__ == '__main__': unittest.main()
