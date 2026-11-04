import base64
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import time
import unittest
import zipfile
from types import SimpleNamespace

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'scripts'))
sys.path.insert(0,str(ROOT/'ramtop'))
from launcher_agent import LauncherTelemetry
from ramtop.bundle import encoded_runtime
from ramtop.telemetry import brief, valid_snapshot


def sample(at):
 return {'schema_version':1,'sampled_at':at,'status':'live','scope':'visible kernel counters','memory':{'dram_read_bytes_per_second':None},
         'processes':[{'pid':10,'name':'worker'}],'connections':[],'lan':[],'filesystems':[]}


class PerformanceTests(unittest.TestCase):
 def test_embedded_runtime_has_no_psutil_or_textual_dependency(self):
  encoded=encoded_runtime(ROOT/'ramtop/ramtop')
  self.assertEqual(encoded,encoded_runtime(ROOT/'ramtop/ramtop'))
  with zipfile.ZipFile(io.BytesIO(base64.b64decode(encoded))) as archive:
   self.assertIn('ramtop/telemetry.py',archive.namelist())
  # -S excludes site-packages: node headless collection must stay standard-library only.
  import tempfile
  with tempfile.TemporaryDirectory() as folder:
   archive=Path(folder)/'runtime.zip';archive.write_bytes(base64.b64decode(encoded))
   result=subprocess.run([sys.executable,'-S','-c',
    'import sys;sys.path.insert(0,sys.argv[1]);from ramtop.telemetry import PerformanceSampler;p=PerformanceSampler();v=p.sample();p.close();print(v["schema_version"])',str(archive)],capture_output=True,text=True,timeout=15)
   self.assertEqual(result.returncode,0,result.stderr)
   self.assertEqual(result.stdout.strip(),'1')

 def test_extended_data_uses_authenticated_compatible_envelope_and_history_is_small(self):
  source=LauncherTelemetry(SimpleNamespace());sink=LauncherTelemetry(SimpleNamespace());now=time.time()
  source.record('performance',sample(now),now)
  source.record('cpu',{'status':'live','sampled_at':now,'cpus':4,'percent':20},now)
  batch=source.pending(set())
  self.assertEqual([row['kind'] for row in batch['samples']],['cpu'])
  sink.accept('owned-node',batch)
  view=sink.fleet({'owned-node'})
  self.assertEqual(view['hosts'][0]['host'],'owned-node')
  self.assertEqual(view['hosts'][0]['performance']['processes'][0]['pid'],10)
  self.assertNotIn('processes',view['events'][0]['hosts'][0]['performance'])
  self.assertEqual(sink.fleet({'other'})['hosts'],[])

 def test_large_replay_batches_obey_count_and_byte_limits(self):
  source=LauncherTelemetry(SimpleNamespace());now=time.time()
  perf=sample(now);perf['processes']=[{'name':'x'*1000,'pid':i} for i in range(48)]
  for _ in range(20):
   source.record('performance',perf,now)
   source.record('cpu',{'status':'live','sampled_at':now,'cpus':4,'percent':20},now)
  batch=source.pending(set())
  self.assertLessEqual(len(batch['samples']),16)
  self.assertLessEqual(len(json.dumps(batch)),250000)
  self.assertGreater(len(batch['samples']),0)

 def test_schema_and_history_keep_limits_and_no_process_details(self):
  value=sample(time.time());self.assertTrue(valid_snapshot(value))
  self.assertNotIn('processes',brief(value))
  value['processes']=[{}]*49;self.assertFalse(valid_snapshot(value))
  value=sample(time.time());value['memory']['x']=float('nan')
  with self.assertRaises(ValueError):valid_snapshot(value)

class PublishIntegrationTests(unittest.TestCase):
 def test_real_encrypted_monitor_publish_and_dedup(self):
  import asyncio
  import threading
  import tempfile
  from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
  from unittest.mock import patch
  from launcher_agent import Agent
  from ramtop.swarm import Publisher
  with tempfile.TemporaryDirectory() as folder:
   config=Path(folder)/'peers.json';master=Path(folder)/'master.json'
   values={'name':'worker','master':'master','peers':{'worker':'10.0.0.2','master':'10.0.0.1'},'key':bytes(range(32)).hex()}
   config.write_text(json.dumps(values));master.write_text(json.dumps({**values,'name':'master'}))
   agent=Agent.__new__(Agent);agent.config=master;agent.lock=threading.RLock();agent.nonce_lock=threading.Lock();agent.nonces={};agent.telemetry=LauncherTelemetry(agent)
   class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
     body=self.rfile.read(int(self.headers['Content-Length']))
     result=asyncio.run(agent.process(body));self.send_response(200);self.end_headers();self.wfile.write(result)
    def log_message(self,*args):pass
   server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
   thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
   import http.client
   connection=http.client.HTTPConnection
   try:
    with patch('ramtop.swarm.http.client.HTTPConnection',side_effect=lambda *args,**kwargs:connection('127.0.0.1',server.server_port,timeout=5)):
     publisher=Publisher(config);publisher.publish(sample(time.time()))
     self.assertEqual(len(publisher.pending),0)
    result=agent.telemetry.fleet({'worker'})
    self.assertEqual(result['hosts'][0]['host'],'worker')
    self.assertEqual(result['hosts'][0]['performance']['processes'][0]['pid'],10)
   finally:server.shutdown();server.server_close();thread.join(2)
