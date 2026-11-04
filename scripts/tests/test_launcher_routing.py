import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from launcher_registry import lan_paths, reconcile_lan_routing


def interface(name,index,address,state='UP'):
    return {'ifname':name,'ifindex':index,'operstate':state,
            'addr_info':[{'family':'inet','scope':'global','local':address,'prefixlen':24}]}


class RoutingTests(unittest.TestCase):
    peers={'master.local':'10.42.0.1','mini.local':'10.42.0.166'}

    def test_physical_lan_paths_exclude_unrelated_wifi_bridges_and_disconnected_links(self):
        rows=[interface('wlp2s0',3,'10.42.0.221'),interface('enp1s0',2,'10.42.0.166'),
              interface('docker0',4,'10.42.0.9'),interface('wlp6s0',5,'172.20.20.20'),
              interface('eth9',6,'10.42.0.12','DOWN')]
        paths=lan_paths(rows,self.peers)
        self.assertEqual([p['device'] for p in paths],['enp1s0','wlp2s0'])
        self.assertEqual([p['network'] for p in paths],['10.42.0.0/24']*2)

    def fake_ip(self,rules=None,endpoints=None,fail_add=False,rows=None,routes=None):
        self.commands=[]
        if rows is None:rows=[interface('enp1s0',2,'10.42.0.166')]
        def run(command,**kwargs):
            self.commands.append(command)
            args=command[1:]
            outputs={('-j','-4','addr'):rows,('-N','-j','rule','show'):rules or [],
                     ('-N','-j','route','show','table','all'):routes or [],
                     ('-j','mptcp','endpoint','show'):endpoints or []}
            if tuple(args) in outputs:return subprocess.CompletedProcess(command,0,json.dumps(outputs[tuple(args)]),'')
            if args==['mptcp','limits','show']:return subprocess.CompletedProcess(command,0,'add_addr_accepted 0 subflows 2','')
            if fail_add and args[:3]==['mptcp','endpoint','add'] and 'signal' in args:
                return subprocess.CompletedProcess(command,1,'','unsupported flags')
            return subprocess.CompletedProcess(command,0,'','')
        return run

    def test_existing_iproute_json_rule_is_idempotent_and_limits_allow_remote_addresses(self):
        rules=[{'priority':24002,'src':'10.42.0.166','dst':'10.42.0.0',
                'dstlen':24,'table':'42002','protocol':'242'}]
        endpoint={'address':'10.42.0.166','id':3,'dev':'enp1s0','signal':True,'subflow':True}
        with patch('subprocess.run',side_effect=self.fake_ip(rules,[endpoint])),patch('subprocess.check_output',return_value='ESTAB'):
            result=reconcile_lan_routing(self.peers)
        self.assertEqual(result['status'],'configured')
        self.assertFalse(any(command[1:3]==['rule','add'] for command in self.commands))
        self.assertIn(['ip','mptcp','limits','set','subflows','4','add_addr_accepted','4'],self.commands)
        self.assertFalse(any('default' in command or 'flush' in command for command in self.commands))

    def test_failed_endpoint_upgrade_restores_networkmanager_endpoint(self):
        endpoint={'address':'10.42.0.166','id':3,'dev':'enp1s0','subflow':True}
        with patch('subprocess.run',side_effect=self.fake_ip(endpoints=[endpoint],fail_add=True)):
            with self.assertRaisesRegex(RuntimeError,'unsupported flags'):reconcile_lan_routing(self.peers)
        self.assertEqual(self.commands[-1],['ip','mptcp','endpoint','add','10.42.0.166','dev','enp1s0','id','3','subflow'])

    def test_foreign_rule_priority_is_never_overwritten(self):
        rules=[{'priority':24002,'src':'all','table':'main'}]
        with patch('subprocess.run',side_effect=self.fake_ip(rules)):
            with self.assertRaisesRegex(RuntimeError,'already in use'):reconcile_lan_routing(self.peers)
        self.assertFalse(any(command[1:3]==['rule','del'] for command in self.commands))

    def test_unplug_removes_only_owned_source_rules_routes_and_endpoints(self):
        rules=[{'priority':24002,'src':'10.42.0.166','dst':'10.42.0.0','dstlen':24,
                'table':'42002','protocol':'242'},
               {'priority':12000,'src':'all','table':'main'}]
        routes=[{'table':'42002','protocol':'242','dst':'10.42.0.0/24',
                 'dev':'enp1s0','prefsrc':'10.42.0.166'},
                {'table':'254','dst':'default','dev':'wlp2s0','protocol':'16'}]
        endpoints=[{'id':160,'address':'10.42.0.166'}, {'id':2,'address':'172.20.20.20','subflow':True}]
        with patch('subprocess.run',side_effect=self.fake_ip(rules,endpoints,rows=[],routes=routes)),patch('subprocess.check_output',return_value=''):
            self.assertEqual(reconcile_lan_routing(self.peers)['status'],'no-lan-paths')
        self.assertIn(['ip','rule','del','priority','24002','from','10.42.0.166','to','10.42.0.0/24','table','42002','protocol','242'],self.commands)
        self.assertIn(['ip','route','del','10.42.0.0/24','table','42002','protocol','242'],self.commands)
        self.assertIn(['ip','mptcp','endpoint','delete','id','160'],self.commands)
        self.assertNotIn(['ip','mptcp','endpoint','delete','id','2'],self.commands)


if __name__=='__main__':unittest.main()
