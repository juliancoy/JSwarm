"""Install checksum-bound GPU bundles through the pinned owner deployment channel."""
import base64
import hashlib
import json
from pathlib import Path
import re
import subprocess
import tempfile

INSTALL=r'''
import base64,hashlib,json,os,pathlib,sys,tempfile
p=json.load(sys.stdin)
if hashlib.sha256(pathlib.Path('/etc/machine-id').read_bytes()).hexdigest()!=p['machine']:raise ValueError('Runtime node identity changed')
root=pathlib.Path.home()/'.local/state/lifetech-launcher/gpu-runtimes';root.mkdir(mode=0o700,parents=True,exist_ok=True)
target=root/p['key']
if not target.exists():
 with tempfile.TemporaryDirectory(prefix='.staging-',dir=root) as tmp:
  folder=pathlib.Path(tmp)
  for name,value in p['files'].items():
   data=base64.b64decode(value)
   if hashlib.sha256(data).hexdigest()!=p['manifest'][name]:raise ValueError('Runtime checksum differs')
   path=folder/name;path.write_bytes(data);path.chmod(0o500)
  (folder/'manifest.json').write_text(json.dumps(p['manifest'],sort_keys=True))
  folder.rename(target)
if any(not (target/n).is_file() or (target/n).is_symlink() or hashlib.sha256((target/n).read_bytes()).hexdigest()!=sha for n,sha in p['manifest'].items()):raise ValueError('Installed runtime was modified')
print(json.dumps({'runtime_directory':str(target),'runtime_files':p['manifest'],'key':p['key'],'execution_owner':'jswarm-node'}))
'''


def install(host,descriptor):
    """The workload pins image paths and digests; JSwarm only stages artifacts."""
    from scripts.swarm_runtime import ROOT,CONFIG
    from agent_updates import LauncherUpdates
    image=descriptor['image'];files=descriptor['files'];expected=descriptor['sha256']
    if not re.fullmatch(r'sha256:[a-f0-9]{64}',image) or not files or len(files)>64 or set(files)!=set(expected):raise ValueError('Invalid pinned runtime source')
    if any(not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}',n) or not isinstance(p,str) or not p.startswith('/') or '..' in Path(p).parts or not re.fullmatch(r'[a-f0-9]{64}',expected[n]) for n,p in files.items()):raise ValueError('Invalid runtime file binding')
    container=subprocess.check_output(['docker','create',image],text=True).strip();encoded={}
    try:
        with tempfile.TemporaryDirectory(prefix='node-gpu-runtime-') as tmp:
            for name,source in files.items():
                path=Path(tmp)/name
                subprocess.run(['docker','cp',container+':'+source,str(path)],capture_output=True,check=True,timeout=30)
                data=path.read_bytes()
                if hashlib.sha256(data).hexdigest()!=expected[name]:raise ValueError('Source image artifact differs from qualification: '+name)
                encoded[name]=base64.b64encode(data).decode()
    finally:subprocess.run(['docker','rm',container],capture_output=True,check=True,timeout=30)
    key=hashlib.sha256(json.dumps(expected,sort_keys=True,separators=(',',':')).encode()).hexdigest()
    payload={'machine':host['machine'],'key':key,'manifest':expected,'files':encoded}
    # This is administrative provisioning, like node/source deployment. It must
    # not queue behind long-lived scientific command pipes. The pinned owner
    # transport and installer both attest the enrolled machine identity.
    config=json.loads(CONFIG.read_text())
    host={**host,'lan_address':config['peers'][host['host']]}
    deployer=LauncherUpdates.__new__(LauncherUpdates)
    try:
        result=subprocess.check_output(deployer.command(host,INSTALL),input=json.dumps(payload),text=True,stderr=subprocess.PIPE,timeout=60)
    except subprocess.CalledProcessError as error:
        if error.returncode!=255 or host.get('local'):raise
        # Some enrolled nodes accept owner SSH only over their LAN IPv6 address.
        # DNS changes transport only: strict pinned host keys and the installer
        # machine attestation remain mandatory.
        fallback={**host,'lan_address':host['host']}
        result=subprocess.check_output(deployer.command(fallback,INSTALL),input=json.dumps(payload),text=True,stderr=subprocess.PIPE,timeout=60)
    return json.loads(result)
