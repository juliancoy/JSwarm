import time
import unittest
from resource_server import merge_performance
from resource_history import project_host

class PerformanceResourceTests(unittest.TestCase):
 def test_independent_performance_survives_new_cpu_frames_and_keeps_its_timestamp(self):
  cache={};old={'schema_version':1,'sampled_at':10,'memory':{'cached_bytes':1}}
  merge_performance([{'host':'node','performance':old}],cache)
  rows=merge_performance([{'host':'node','sampled_at':20}],cache)
  self.assertEqual(rows[0]['performance']['sampled_at'],10)
  new={'schema_version':1,'sampled_at':30,'memory':{'cached_bytes':2}}
  self.assertEqual(merge_performance([{'host':'node','performance':new}],cache)[0]['performance'],new)

 def test_history_omits_processes_connections_and_raw_per_cpu_counters(self):
  result=project_host({'host':'node','performance':{'schema_version':1,'sampled_at':time.time(),
   'memory':{'cached_bytes':1,'counters':[{'name':'cpu0','value':1}]},'processes':[{'pid':123}],
   'connections':[{'remote':'private'}],'lan':[{'address':'private'}]}})
  self.assertNotIn('processes',result['performance'])
  self.assertNotIn('connections',result['performance'])
  self.assertNotIn('counters',result['performance']['memory'])
  self.assertEqual(result['performance']['memory']['cached_bytes'],1)
