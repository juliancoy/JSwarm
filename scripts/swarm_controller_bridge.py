"""Explicit SSH bootstrap to an existing controller; signing keys stay there.

Workers are still addressed through the controller's enrolled, signed node API.
This is not SSH fallback to a worker or a replacement node protocol.
"""
import json
import shlex
import subprocess

RPC='''import json,sys
from pathlib import Path
from scripts.swarm_stream_work import StreamClient
from scripts.swarm_runtime import CONFIG
p=json.load(sys.stdin);peer=p['worker'];address=json.loads(CONFIG.read_text())['peers'][peer['host']]
if isinstance(address,dict):address=address['address']
peer['agent_endpoint']='http://'+address+':8790'
with StreamClient(peer) as client:
 if p['operation']=='capabilities':result=client.capabilities()
 elif p['operation']=='submit':result=client.submit(p['arguments'])
 elif p['operation']=='status':result=client.status(p['arguments']['id'])
 elif p['operation']=='cancel':result=client.call('stream.cancel',p['arguments'])
 else:raise ValueError('Unsupported controller operation')
print(json.dumps(result))
'''


class ControllerBridge:
    def __init__(self,target,worker,workspace='/home/julian/Documents/Organization',python='/home/julian/global/bin/python'):
        if not target or target.startswith('-') or any(c.isspace() for c in target):raise ValueError('Invalid explicit controller target')
        self.target=target;self.worker=dict(worker);self.workspace=workspace;self.python=python

    def invoke(self,operation,arguments=None):
        command='cd '+shlex.quote(self.workspace)+' && '+shlex.join([self.python,'-c',RPC])
        result=subprocess.run(['ssh','-o','BatchMode=yes','-o','StrictHostKeyChecking=yes','-o','ConnectTimeout=5',
                               self.target,command],input=json.dumps({'worker':self.worker,'operation':operation,'arguments':arguments}),
                              text=True,capture_output=True,timeout=45)
        if result.returncode:raise RuntimeError('Existing controller operation failed')
        return json.loads(result.stdout)

    def capabilities(self):return self.invoke('capabilities')
    def submit(self,spec):return self.invoke('submit',spec)
    def status(self,ident):return self.invoke('status',{'id':ident})
    def call(self,operation,args):
        if operation!='stream.cancel':raise ValueError('Unsupported bridge operation')
        return self.invoke('cancel',args)
    def close(self):pass
