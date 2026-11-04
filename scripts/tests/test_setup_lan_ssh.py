import importlib.util, unittest, tempfile, json, sys
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace
spec=importlib.util.spec_from_file_location('setup', Path(__file__).resolve().parents[1] / 'setup-lan-ssh.py')
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)

class SetupTests(unittest.TestCase):
 def exercise(self, answers=(), codes=(0,), discovery=None):
  with tempfile.TemporaryDirectory() as d:
   root=Path(d);key=root/'key';key.write_text('fixture-not-a-real-key')
   argv=['setup','--key',str(key),'--config',str(root/'config.json'),'--report',str(root/'report.json')]
   if discovery is None:argv+=['julian@example.local']
   with patch.object(sys,'argv',argv),patch.object(sys.stdin,'isatty',return_value=True),patch.object(m.shutil,'which',return_value='/bin/fixture'),patch.object(m,'resolved_addresses',return_value=set()),patch.object(m,'ssh_port',return_value='open'),patch.object(m,'run',side_effect=[SimpleNamespace(returncode=c) for c in codes]) as run,patch.object(m,'prepare_key',return_value=True) as prepare,patch('builtins.input',side_effect=answers),patch.object(m,'discover',return_value=discovery),patch('builtins.print'):
    result=m.main()
   return result,json.loads((root/'report.json').read_text()),json.loads((root/'config.json').read_text()) if (root/'config.json').exists() else {},run.call_args_list,prepare.call_count
 def test_existing_key_is_verified_without_prompts_or_install(self):
  result,report,config,calls,prepares=self.exercise()
  self.assertEqual(result,0);self.assertEqual(prepares,0);self.assertEqual(len(calls),1);self.assertEqual(report['devices'][0]['status'],'key access verified')
 def test_skip_once_does_not_persist_exclusion(self):
  result,report,config,calls,prepares=self.exercise(['s'],[255])
  self.assertEqual(report['devices'][0]['status'],'skipped');self.assertFalse(config);self.assertEqual(prepares,0)
 def test_change_username_verifies_before_install(self):
  _,report,config,calls,prepares=self.exercise(['c','alice'],[255,0])
  self.assertEqual(config['accounts']['example.local'],'alice');self.assertEqual(prepares,0)
 def test_install_requires_explicit_action(self):
  _,report,config,calls,prepares=self.exercise(['i'],[255,0,0])
  self.assertEqual(prepares,1);self.assertEqual(calls[1].args[0][0],'ssh-copy-id');self.assertEqual(report['devices'][0]['status'],'key access verified')
 def test_unknown_device_is_skipped_without_prompt(self):
  _,report,config,calls,prepares=self.exercise([],[],([], [{'address':'10.0.0.2','mac':'aa:bb:cc:dd:ee:ff'}]))
  self.assertEqual(report['devices'][0]['status'],'skipped: no mDNS name');self.assertFalse(calls);self.assertFalse(config)
 def test_ipv6_mdns_resolves_device_with_unanswered_ipv4(self):
  links=[{'ifname':'enp7s0','operstate':'UP','addr_info':[{'local':'10.42.0.1'}]}]
  peers=[{'dev':'enp7s0','dst':'10.42.0.221','lladdr':'10:6f:d9:3c:5b:95'},
         {'dev':'enp7s0','dst':'fe80::e719:fcc6:f761:9257','lladdr':'10:6f:d9:3c:5b:95'}]
  def output(command,**kwargs): return json.dumps(peers if 'neigh' in command else links)
  def run(command,**kwargs):
   if command[0]=='avahi-browse': return SimpleNamespace(returncode=0,stdout='',stderr='')
   if command[-1].startswith('fe80:'): return SimpleNamespace(returncode=0,stdout=command[-1]+'\tjulian-minipc.local\n')
   return SimpleNamespace(returncode=1,stdout='')
  with patch.object(m,'run',side_effect=run),patch.object(m.subprocess,'check_output',side_effect=output):
   names,missing=m.discover()
  self.assertEqual(names,['julian-minipc.local']);self.assertEqual(missing,[])
 def test_ipv4_and_ipv6_without_mdns_produce_one_skipped_device(self):
  links=[{'ifname':'enp7s0','operstate':'UP','addr_info':[{'local':'10.42.0.1'}]}]
  peers=[{'dev':'enp7s0','dst':'10.42.0.2','lladdr':'aa:bb:cc:dd:ee:ff'},
         {'dev':'enp7s0','dst':'fe80::2','lladdr':'aa:bb:cc:dd:ee:ff'}]
  def output(command,**kwargs): return json.dumps(peers if 'neigh' in command else links)
  with patch.object(m,'run',return_value=SimpleNamespace(returncode=0,stdout='',stderr='')),patch.object(m.subprocess,'check_output',side_effect=output):
   names,missing=m.discover()
  self.assertEqual(names,[]);self.assertEqual(len(missing),1);self.assertEqual(len(missing[0]['addresses']),2)

if __name__ == '__main__':
 unittest.main()
