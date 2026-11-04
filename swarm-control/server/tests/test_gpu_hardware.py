import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock,patch
from gpu_worker import GpuRun
sys.path.insert(0,str(Path(__file__).resolve().parents[3]/'scripts'))
from launcher_agent import hardware_sample,Agent,GPU_SMOKE,UNPRIVILEGED_OPS


class GpuHardwareTests(unittest.TestCase):
    def test_inventory_is_not_compute_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);device=root/'0000:01:00.0';device.mkdir()
            for name,value in {'class':'0x030200','vendor':'0x10de','device':'0x1234'}.items():(device/name).write_text(value)
            with patch('launcher_agent.shutil.which',return_value=None):value=hardware_sample(root,root)
            self.assertEqual(value['gpus'][0]['vendor'],'NVIDIA')
            self.assertNotIn('gpu_validation',value)
            self.assertGreater(value['storage']['free_bytes'],0)

    def test_only_runtime_ready_cuda_hosts_get_kernel_jobs(self):
        service=Mock();service.lock=threading.RLock();service.run={}
        hosts=[{'host':name,'suites':[],'jobs':{},'agent_status':{'hardware':hardware}} for name,hardware in [
            ('ready',{'gpus':[{'vendor':'NVIDIA'}],'cuda_container_runtime':True}),
            ('blocked',{'gpus':[{'vendor':'NVIDIA'}],'cuda_container_runtime':False}),
            ('intel',{'gpus':[{'vendor':'Intel'}],'cuda_container_runtime':False})]]
        client=Mock();client.call.return_value={'status':'passed','devices':[{'name':'Test GPU','kernel_result':42}]}
        with patch('gpu_worker.AgentClient',return_value=client):
            run=GpuRun(service,hosts,Mock(),True);run.join()
        self.assertEqual(hosts[0]['jobs']['gpu']['status'],'passed')
        self.assertEqual(hosts[1]['jobs'],{})
        self.assertEqual(hosts[2]['jobs'],{})
        self.assertEqual(service.run['gpu']['coverage'][1]['reason'],'NVIDIA runtime missing')

    def test_probe_is_controller_signed_fixed_code_and_single_flight(self):
        self.assertNotIn('gpu.probe',UNPRIVILEGED_OPS)
        agent=Agent.__new__(Agent);agent.gpu_lock=threading.Lock()
        with self.assertRaises(ValueError):agent.dispatch('gpu.probe',{'image':'untrusted'})
        agent.gpu_lock.acquire()
        try:
            with self.assertRaisesRegex(RuntimeError,'already running'):agent.dispatch('gpu.probe',{})
        finally:agent.gpu_lock.release()
        self.assertIn('cuLaunchKernel',GPU_SMOKE)
        self.assertIn('output.value!=42',GPU_SMOKE)
