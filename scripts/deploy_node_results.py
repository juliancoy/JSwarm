"""Install JSwarm's independent mTLS result sink without restarting input I/O."""
import json
from pathlib import Path
import subprocess
import sys
from scripts.swarm_runtime import shell_command

INSTALL=r'''
import hashlib,json,os,pathlib,subprocess,sys
p=json.loads(sys.argv[1]);home=pathlib.Path.home();base=home/'.local/state/lifetech-launcher/bulk-transport'
config=json.loads((base/'config.json').read_text())
if config['machine']!=p['machine'] or hashlib.sha256(pathlib.Path('/etc/machine-id').read_bytes()).hexdigest()!=p['machine']:raise ValueError('Result owner machine changed')
compile(p['source'],'result-transfer.py','exec');revision=hashlib.sha256((p['source']+p['bulk']+'node-results-container-v2').encode()).hexdigest()
release=base/'result-runtime'/revision;release.mkdir(parents=True,exist_ok=True,mode=0o700)
source=release/'transfer.py'
if source.exists() and source.read_text()!=p['source']:raise ValueError('Immutable result runtime changed')
source.write_text(p['source']);source.chmod(0o600);(base/'inboxes').mkdir(exist_ok=True,mode=0o700)
# Make grant creation available through the existing owner command interface.
bulk=base/'current/bulk.py';compile(p['bulk'],'bulk.py','exec')
# Do not mutate an immutable input-service release. A separate tool entry point
# imports the new grant helper from its own content-addressed source directory.
(release/'bulk.py').write_text(p['bulk']);(release/'bulk.py').chmod(0o600)
port=config['port']+1
# The node owner already provides access to its local Docker daemon. Keep the
# result service independent of the owner's login session and systemd user bus.
unit=home/'.config/systemd/user/lifetech-result-https.service'
if unit.exists() and unit.read_text().startswith('# managed-by=run.py; service=jswarm-node-results'):
 if p.get('local'):subprocess.run(['systemctl','--user','disable','--now',unit.name],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
 unit.unlink()
image=None
for tag in ('organization-launcher:webrtc-1','python:3.12-bookworm'):
 probe=subprocess.run(['docker','image','inspect',tag,'--format','{{.Id}}'],capture_output=True,text=True)
 if probe.returncode==0:image=probe.stdout.strip();break
if not image:raise RuntimeError('No local approved Python node runtime image')
security=json.loads(subprocess.check_output(['docker','info','--format','{{json .SecurityOptions}}'],text=True))
rootless=any('rootless' in item for item in security);user='0:0' if rootless else str(os.getuid())+':'+str(os.getgid())
outputs=base.parent/'wgs-alignments';validation=base.parent/'node-alignment-validation'
for directory in (outputs,validation):directory.mkdir(parents=True,exist_ok=True,mode=0o700)
name='swarm-node-results';probe=subprocess.run(['docker','inspect',name],capture_output=True,text=True)
current=json.loads(probe.stdout)[0] if probe.returncode==0 else None
if current:
 labels=current['Config'].get('Labels') or {}
 if labels.get('organization.managed-by')!='run.py' or labels.get('org.lifetech.service')!='node-results':raise ValueError('Unrecognized result container ownership')
 if labels.get('org.lifetech.revision')!=revision or current['Image']!=image:
  subprocess.run(['docker','rm','-f',name],check=True,stdout=subprocess.DEVNULL);current=None
if not current:
 argv=['docker','run','-d','--name',name,'--restart','unless-stopped','--network','host','--read-only','--user',user,'--cap-drop','ALL','--security-opt','no-new-privileges','--memory','256m','--memory-swap','256m','--cpus','1','--pids-limit','64','--label','organization.managed-by=run.py','--label','org.lifetech.service=node-results','--label','org.lifetech.revision='+revision,'--env','PYTHONDONTWRITEBYTECODE=1','--log-opt','max-size=1m','--log-opt','max-file=2']
 for source_path,readonly in ((base,True),(outputs,False),(validation,False)):
  argv+=['--mount','type=bind,src='+str(source_path)+',dst='+str(source_path)+(',readonly' if readonly else '')]
 health="import ssl,http.client; c=ssl.create_default_context(cafile="+repr(str(base/'ca.crt'))+"); c.check_hostname=False; c.load_cert_chain("+repr(str(base/'identity.crt'))+","+repr(str(base/'identity.key'))+"); h=http.client.HTTPSConnection("+repr(config['address'])+","+str(port)+",context=c,timeout=3); h.request('GET','/objects/node-health'); r=h.getresponse(); assert r.status==404; r.read(); h.close()"
 import shlex
 argv+=['--health-cmd',shlex.join(['python3','-c',health]),'--health-interval','10s','--health-timeout','5s','--health-start-period','10s','--health-retries','3']
 argv+=['--entrypoint','python3',image,str(source),'serve','--bind',config['address'],'--port',str(port),'--manifest',str(base/'objects'),'--ca',str(base/'ca.crt'),'--cert',str(base/'identity.crt'),'--key',str(base/'identity.key')]
 subprocess.run(argv,check=True,stdout=subprocess.DEVNULL)
elif not current['State']['Running']:subprocess.run(['docker','start',name],check=True,stdout=subprocess.DEVNULL)
print(json.dumps({'root':str(release),'port':port,'revision':revision,'node':config['name']}))
'''
PORTS=r'''
import json,os,pathlib,sys
p=json.loads(sys.argv[1]);base=pathlib.Path.home()/'.local/state/lifetech-launcher/bulk-transport';path=base/'config.json';config=json.loads(path.read_text())
for name,port in p.items():config['peers'][name]['output_port']=port
for target,value in ((path,config),(pathlib.Path.home()/'.config/lifetech-launcher/bulk-streams.json',None)):
 if value is None:
  value=json.loads(target.read_text());value['config']=config
 temporary=target.with_suffix('.results.tmp');temporary.write_text(json.dumps(value));temporary.chmod(0o600);os.replace(temporary,target)
'''


def install(host):
    root=Path(__file__).resolve().parent
    payload={'local':bool(host.get('local')),'machine':host['machine'],'source':(root/'swarm_tls_transfer.py').read_text(),'bulk':(root/'swarm_bulk.py').read_text()}
    result=subprocess.run(shell_command(host,['python3','-c',INSTALL,json.dumps(payload)]),capture_output=True,text=True,timeout=45)
    if result.returncode:raise RuntimeError('Node result service installation failed on '+host['host']+': '+result.stderr[-1000:])
    return json.loads(result.stdout)


def configure_peers(host,ports):
    subprocess.run(shell_command(host,['python3','-c',PORTS,json.dumps(ports)]),capture_output=True,text=True,check=True,timeout=30)
