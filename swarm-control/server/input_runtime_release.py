"""Immutable input-runtime releases using the standard launcher SSH deploy path."""
import hashlib
import json
from pathlib import Path
import subprocess

FILES=('__init__.py','run_inputs.py','run_input_cache.py','run_cache_peers.py','run_input_source.py',
       'venter_stream_offload.py','run_manager.py','huref_compare_worker.py','huref_compare_swarm.py',
       'huref_followup_worker.py','huref_followup_swarm.py','huref_projected_worker.py',
       'huref_projected_swarm.py','huref_evidence.py')

INSTALL=r'''import hashlib,json,os,pathlib,sys,tempfile
p=json.load(sys.stdin)
if hashlib.sha256(pathlib.Path('/etc/machine-id').read_bytes()).hexdigest()!=p['machine']:raise ValueError('Machine identity changed')
config=pathlib.Path(os.environ.get('LIFETECH_PEER_CONFIG',str(pathlib.Path.home()/'.config/lifetech-launcher/peers.json')))
if json.loads(config.read_text())['name']!=p['node']:raise ValueError('Wrong enrolled owner')
files=p['files']
expected={'__init__.py','run_inputs.py','run_input_cache.py','run_cache_peers.py','run_input_source.py','venter_stream_offload.py','run_manager.py','huref_compare_worker.py','huref_compare_swarm.py','huref_followup_worker.py','huref_followup_swarm.py','huref_projected_worker.py','huref_projected_swarm.py','huref_evidence.py'}
if set(files)!=expected:raise ValueError('Incomplete input release')
manifest={name:hashlib.sha256(text.encode()).hexdigest() for name,text in files.items()}
revision=hashlib.sha256(json.dumps(manifest,sort_keys=True).encode()).hexdigest()
if revision!=p['revision']:raise ValueError('Input release digest mismatch')
for name,text in files.items():compile(text,name,'exec')
base=config.parent/'input-releases';base.mkdir(mode=0o700,exist_ok=True)
target=base/revision
if not target.exists():
 with tempfile.TemporaryDirectory(dir=base,prefix='.staging-') as temp:
  folder=pathlib.Path(temp);scripts=folder/'scripts';scripts.mkdir(mode=0o700)
  for name,text in files.items():(scripts/name).write_text(text)
  (folder/'manifest.json').write_text(json.dumps(manifest,sort_keys=True))
  folder.rename(target)
for name,digest in manifest.items():
 if hashlib.sha256((target/'scripts'/name).read_bytes()).hexdigest()!=digest:raise ValueError('Installed release corrupt')
current=base/'current'
if current.is_symlink() and current.resolve()!=target:
 previous=base/'previous.tmp';previous.unlink(missing_ok=True);previous.symlink_to(current.resolve().name);previous.replace(base/'previous')
pending=base/'current.tmp';pending.unlink(missing_ok=True);pending.symlink_to(revision);pending.replace(current)
print(json.dumps({'revision':revision,'verified':True,'active_jobs_restarted':False}))
'''

class InputRuntimeRelease:
    def __init__(self,root):
        sources=Path(root)/'scripts'
        # The split workspace keeps this legacy, application-owned release's
        # source paths in the Organization scripts compatibility directory.
        compatibility=Path(root).parent/'scripts'
        if not all((sources/name).is_file() for name in FILES) and all((compatibility/name).is_file() for name in FILES):
            sources=compatibility
        self.files={name:(sources/name).read_text() for name in FILES}
        manifest={name:hashlib.sha256(text.encode()).hexdigest() for name,text in self.files.items()}
        self.revision=hashlib.sha256(json.dumps(manifest,sort_keys=True).encode()).hexdigest()

    def stage(self,updater,host,status):
        payload={'files':self.files,'revision':self.revision,'node':status['node'],'machine':status['machine']}
        result=subprocess.run(updater.command(host,INSTALL),input=json.dumps(payload),capture_output=True,text=True,timeout=40)
        if result.returncode:raise RuntimeError('Input release staging failed; inspect enrolled host access')
        receipt=json.loads(result.stdout)
        if receipt.get('revision')!=self.revision or receipt.get('verified') is not True:raise RuntimeError('Input release receipt mismatch')
        return receipt
