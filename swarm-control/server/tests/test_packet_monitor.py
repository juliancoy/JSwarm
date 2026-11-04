import ipaddress
import struct
import unittest
from unittest.mock import patch
from packet_monitor import parse_frame, internet_destination, attribute, Destinations, MAX_PAIRS, Flows, flow_observation

HOST = 'f0:2f:74:1d:9c:b1'
CLIENT = '10:6f:d9:3c:5b:95'

def ethernet(payload, kind=0x0800, tags=0):
    start = bytes.fromhex(HOST.replace(':','')+CLIENT.replace(':',''))
    return start + (struct.pack('!HH',0x8100,1)*tags) + struct.pack('!H',kind) + payload


def ipv4(src='10.42.0.221', dst='1.1.1.1', flags=0, options=b'', payload=b'hello'):
    return struct.pack('!BBHHHBBH4s4s',0x45+len(options)//4,0,24+len(options)+len(payload),0,flags,64,6,0,
                       ipaddress.ip_address(src).packed,ipaddress.ip_address(dst).packed)+options+struct.pack('!HH',1234,443)+payload


def ipv6(src='2601:152:300:c16e::a', dst='2606:4700:4700::1111', extension=b'', protocol=17):
    payload = extension+struct.pack('!HH',1234,443)+b'hello'
    return struct.pack('!IHBB16s16s',6<<28,len(payload),protocol,64,ipaddress.ip_address(src).packed,
                       ipaddress.ip_address(dst).packed)+payload


class PacketTests(unittest.TestCase):
    def test_ipv4_vlan_and_header_snap_count_full_ip_bytes(self):
        frame = ethernet(ipv4(options=b'\0'*4, payload=b'x'*1400),tags=2)
        packet = parse_frame(frame[:256],len(frame))
        self.assertEqual(packet['ip_bytes'],1428)
        self.assertEqual(packet['socket'],(6,'10.42.0.221',1234,'1.1.1.1',443))
        self.assertEqual(packet['source_mac'],CLIENT)
        self.assertIsNone(parse_frame(frame[:256]))

    def test_ipv6_extensions_and_fragments(self):
        packet = parse_frame(ethernet(ipv6(extension=bytes([17,0])+b'\0'*6,protocol=0),0x86dd))
        self.assertEqual(packet['socket'][0],17)
        fragmented = parse_frame(ethernet(ipv6(extension=bytes([17,0])+b'\0'*6,protocol=44),0x86dd))
        self.assertIsNone(fragmented['socket'])
        self.assertIsNone(parse_frame(ethernet(ipv4(flags=0x2000)))['socket'])

    def test_malformed_and_truncated_frames_never_raise(self):
        for frame in (ethernet(ipv4()),ethernet(ipv6(),0x86dd),ethernet(ipv4(),tags=2)):
            for end in range(len(frame)):
                self.assertIsNone(parse_frame(frame[:end]))
        bad = bytearray(ethernet(ipv4())); bad[14]=0x41
        self.assertIsNone(parse_frame(bad))
        multicast = bytearray(ethernet(ipv4()));multicast[0]=1
        self.assertIsNone(parse_frame(multicast))
        broken = ethernet(ipv6(extension=bytes([17,200])+b'\0'*6,protocol=0),0x86dd)
        self.assertIsNone(parse_frame(broken))

    def test_internet_filter_includes_connected_global_ipv6_prefix(self):
        networks = [ipaddress.ip_network(n) for n in ('10.42.0.0/24','2601:152:300:c16e::/64')]
        for address in ('10.42.0.1','10.42.0.255','255.255.255.255','224.0.0.1','ff02::1','fe80::1','::1','100.64.0.1','2601:152:300:c16e::b'):
            self.assertFalse(internet_destination(address,networks),address)
        for address in ('1.1.1.1','2606:4700:4700::1111'):
            self.assertTrue(internet_destination(address,networks))

    def test_attribution_pre_nat_and_host_socket_proof(self):
        config={'lan_interface':'lan','interfaces':{'lan':{'mac':HOST,'networks':['10.42.0.0/24']},'wan':{'mac':HOST}},
                'host_addresses':['172.20.20.20'],'host_mac':HOST}
        packet=parse_frame(ethernet(ipv4()))
        self.assertEqual(attribute(packet,'lan',False,config,set())[0],CLIENT)
        self.assertIsNone(attribute(packet,'wan',False,config,set()))
        self.assertIsNone(attribute(packet,'lan',True,config,set()))
        # A NAT-looking source alone cannot be assigned to the gateway.
        translated=parse_frame(ethernet(ipv4(src='172.20.20.20')))
        translated['source_mac']=HOST
        self.assertIsNone(attribute(translated,'wan',True,config,set()))
        self.assertEqual(attribute(translated,'wan',True,config,{translated['socket']})[0],HOST)
        # The original ingress cannot be counted again on forwarded egress.
        self.assertIsNone(attribute(packet,'wan',True,config,{translated['socket']}))
        config['interfaces']['lan']['networks'].append('2601:152:300:c16e::/64')
        global_lan=parse_frame(ethernet(ipv6(),0x86dd))
        self.assertEqual(attribute(global_lan,'lan',False,config,set())[0],CLIENT)

    def test_retention_rates_limits_and_idle_expiry(self):
        records=Destinations();packet=parse_frame(ethernet(ipv4()))
        records.observe(packet,CLIENT,'LAN','lan',1,'first')
        records.observe(packet,CLIENT,'LAN','lan',2,'second')
        row=records.view(2,1)['relationships'][0]
        self.assertEqual(row['bytes'],packet['ip_bytes']*2)
        self.assertEqual(row['mbps'],row['bytes']*8/1e6)
        self.assertEqual(row['first_seen'],'first')
        self.assertEqual(records.view(3,1)['relationships'][0]['mbps'],0)
        self.assertEqual(records.view(302,1)['relationships'],[])
        with patch('packet_monitor.MAX_PAIRS',3):
            for i in range(8):
                packet['destination']='1.1.1.'+str(i)
                records.observe(packet,CLIENT,'LAN','lan',i,'time')
            self.assertEqual(len(records.records),3)
            self.assertEqual(records.evictions,5)
        self.assertTrue(all(not any(k in v for k in ('payload','socket','frame')) for v in records.records.values()))


if __name__ == '__main__':
    unittest.main()


class BidirectionalTests(unittest.TestCase):
    def setUp(self):
        self.config={'lan_interface':'lan','interfaces':{'lan':{'mac':HOST,'networks':['10.42.0.0/24']},'wan':{'mac':HOST}},
                     'local_networks':['10.42.0.0/24','172.20.20.0/24'],'host_addresses':['172.20.20.20'],'host_mac':HOST}

    def test_lan_download_is_attributed_after_nat_only(self):
        packet=parse_frame(ethernet(ipv4(src='1.1.1.1',dst='10.42.0.221')))
        packet['source_mac']=HOST;packet['destination_mac']=CLIENT
        flow=flow_observation(packet,'lan',True,self.config,set())
        self.assertEqual(flow['device_mac'],CLIENT)
        self.assertEqual(flow['direction'],'download')
        self.assertEqual(flow['remote_port'],1234)
        self.assertEqual(flow['local_port'],443)
        self.assertIsNone(flow_observation(packet,'wan',False,self.config,set()))
        self.assertIsNone(flow_observation(packet,'lan',False,self.config,set()))

    def test_host_download_requires_exact_reversed_socket_not_nat_address_alone(self):
        packet=parse_frame(ethernet(ipv4(src='1.1.1.1',dst='172.20.20.20')))
        reverse=(6,'172.20.20.20',443,'1.1.1.1',1234)
        self.assertIsNone(flow_observation(packet,'wan',False,self.config,set()))
        flow=flow_observation(packet,'wan',False,self.config,{reverse})
        self.assertEqual(flow['device_mac'],HOST)
        self.assertEqual(flow['direction'],'download')
        self.assertIsNone(flow_observation(packet,'wan',True,self.config,{reverse}))

    def test_two_directions_join_one_tuple_with_separate_rates_and_evidence(self):
        packet=parse_frame(ethernet(ipv4()))
        upload=flow_observation(packet,'lan',False,self.config,set())
        download={**upload,'direction':'download','basis':'LAN egress after NAT'}
        records=Flows();records.observe(upload,100,1,'first');records.observe(download,900,2,'last')
        row=records.view(2,2)['records'][0]
        self.assertEqual(row['upload_mbps'],.0004)
        self.assertEqual(row['download_mbps'],.0036)
        self.assertEqual(row['download_basis'],'LAN egress after NAT')
        self.assertEqual(records.view(3,1)['records'][0]['download_mbps'],0)
        self.assertEqual(records.view(302,1)['records'],[])

    def test_local_traffic_excluded_and_flow_cache_is_bounded(self):
        packet=parse_frame(ethernet(ipv4(dst='10.42.0.1')))
        self.assertIsNone(flow_observation(packet,'lan',False,self.config,set()))
        packet=parse_frame(ethernet(ipv4()))
        flow=flow_observation(packet,'lan',False,self.config,set());records=Flows()
        with patch('packet_monitor.MAX_PAIRS',2):
            for i in range(4):records.observe({**flow,'local_port':i},100,i,'time')
        self.assertEqual(len(records.records),2)
        self.assertEqual(records.evictions,2)

    def test_ipv6_download_is_accounted_on_lan_egress(self):
        self.config['interfaces']['lan']['networks'].append('2601:152:300:c16e::/64')
        self.config['local_networks'].append('2601:152:300:c16e::/64')
        packet=parse_frame(ethernet(ipv6(src='2606:4700:4700::1111',dst='2601:152:300:c16e::a'),0x86dd))
        packet['source_mac']=HOST;packet['destination_mac']=CLIENT
        flow=flow_observation(packet,'lan',True,self.config,set())
        self.assertEqual(flow['direction'],'download')
        self.assertEqual(flow['protocol'],17)

class UplinkAccountingTests(unittest.TestCase):
    def test_unmatched_nat_packet_visible_without_claiming_host_or_double_counting_lan(self):
        from packet_monitor import uplink_observation
        config={'lan_interface':'lan','interfaces':{'lan':{'mac':HOST,'networks':['10.42.0.0/24']},'wan':{'mac':HOST}},
                'local_networks':['10.42.0.0/24','172.20.20.0/24'],'host_addresses':['172.20.20.20'],'host_mac':HOST}
        packet=parse_frame(ethernet(ipv4(src='172.20.20.20')))
        packet['source_mac']=HOST
        flow=uplink_observation(packet,'wan',True,config,set())
        self.assertEqual(flow['device_mac'],'uplink-unattributed')
        self.assertEqual(flow['direction'],'upload')
        self.assertIsNone(uplink_observation(packet,'lan',True,config,set()))
        known=uplink_observation(packet,'wan',True,config,{packet['socket']})
        self.assertEqual(known['device_mac'],HOST)
        packet['source_ip'],packet['destination']=packet['destination'],packet['source_ip']
        packet['destination_mac']=HOST
        self.assertEqual(uplink_observation(packet,'wan',False,config,set())['direction'],'download')
        self.assertIsNone(uplink_observation(packet,'wan',True,config,set()))

    def test_interface_counter_reset_is_not_a_traffic_spike(self):
        import tempfile
        from pathlib import Path
        from packet_monitor import InterfaceRates
        with tempfile.TemporaryDirectory() as root:
            folder=Path(root)/'wan/statistics';folder.mkdir(parents=True)
            def write(tx,rx):
                (folder/'tx_bytes').write_text(str(tx));(folder/'rx_bytes').write_text(str(rx))
            meter=InterfaceRates(root);write(10,20)
            self.assertEqual(meter.sample(['wan'],1),{})
            write(1000010,2000020)
            self.assertEqual(meter.sample(['wan'],5)['wan'],{'upload_mbps':2,'download_mbps':4})
            write(0,0);self.assertEqual(meter.sample(['wan'],6),{})

class TcpTrafficBreakdownTests(unittest.TestCase):
    def test_ack_only_and_payload_are_distinct(self):
        import struct
        tcp=bytearray(20);struct.pack_into('!HH',tcp,0,1234,443);tcp[12]=0x50;tcp[13]=0x10
        packet=parse_frame(ethernet(ipv4(payload=bytes(tcp[4:]))))
        self.assertTrue(packet['tcp_ack_only']);self.assertEqual(packet['tcp_payload_bytes'],0)
        packet=parse_frame(ethernet(ipv4(payload=bytes(tcp[4:])+b'abcd')))
        self.assertFalse(packet['tcp_ack_only']);self.assertEqual(packet['tcp_payload_bytes'],4)
