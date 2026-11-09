"""Install an independent source metadata container for an existing enrolled node."""
import json
from pathlib import Path
import subprocess

INSTALL=r'''
import hashlib,json,os,pathlib,subprocess,sys
p=json.load(sys.stdin);home=pathlib.Path.home();base=home/'.local/state/lifetech-launcher/bulk-transport'
config=json.loads((base/'config.json').read_text())
if config['name']!=p['node'] or config['machine']!=p['machine'] or hashlib.sha256(pathlib.Path('/etc/machine-id').read_bytes()).hexdigest()!=p['machine']:raise ValueError('Existing source owner identity differs')
if p['controller'] not in config['peers']:raise ValueError('Controller is not enrolled')
inputs=[str(pathlib.Path(path).resolve(strict=True)) for path in p['inputs']]
outputs=[str(pathlib.Path(path).resolve()) for path in p['outputs']]
for path in outputs:pathlib.Path(path).mkdir(parents=True,exist_ok=True,mode=0o700)
(base/'objects').mkdir(exist_ok=True,mode=0o700);(base/'inboxes').mkdir(exist_ok=True,mode=0o700)
policy={'controller':p['controller'],'inputs':inputs,'outputs':outputs,'port':config['port']+2}
if p.get('authorization') is not None:
 auth=p['authorization']
 for field in ('secret_file','approvals_file'):
  path=pathlib.Path(auth[field]).resolve(strict=True)
  if base/'source-authorization' not in path.parents:raise ValueError('Authorization files must be under bulk-transport/source-authorization')
 policy['authorization']=auth
 if p.get('encrypted_catalogue'):
  catalogue=pathlib.Path(p['encrypted_catalogue']).resolve(strict=True)
  if base/'source-authorization' not in catalogue.parents:raise ValueError('Encrypted catalogue must be under protected configuration')
  policy['encrypted_catalogue']=str(catalogue)
revision=hashlib.sha256(json.dumps({'files':p['files'],'policy':policy},sort_keys=True).encode()).hexdigest()
release=base/'source-runtime'/revision;release.mkdir(parents=True,exist_ok=True,mode=0o700)
for name,source in p['files'].items():
 if name not in ('source.py','bulk.py','transfer.py','authorization.py','protected.py','contracts.py','crypto.py'):raise ValueError('Unknown source service file')
 compile(source,name,'exec');path=release/name
 if path.exists() and path.read_text()!=source:raise ValueError('Immutable source service changed')
 path.write_text(source);path.chmod(0o600)
(release/'policy.json').write_text(json.dumps(policy));(release/'policy.json').chmod(0o600)
if 'authorization' in policy:
 subprocess.run(['python3','-c','import sys,json;sys.path.insert(0,sys.argv[1]);from source import SourceService;SourceService(sys.argv[2],json.load(open(sys.argv[3])))',str(release),str(base),str(release/'policy.json')],check=True,capture_output=True)
image=None
for tag in ('organization-swarm-node:1','organization-launcher:webrtc-1','python:3.12-bookworm'):
 result=subprocess.run(['docker','image','inspect',tag,'--format','{{.Id}}'],capture_output=True,text=True)
 if result.returncode==0:image=result.stdout.strip();break
if not image:raise ValueError('Approved existing Python image unavailable')
if 'encrypted_catalogue' in policy:
 subprocess.run(['docker','run','--rm','--network','none','--read-only','--entrypoint','python3',image,'-c','import crypt4gh; import importlib.metadata; assert importlib.metadata.version("crypt4gh")=="1.8.6"'],check=True,capture_output=True)
security=json.loads(subprocess.check_output(['docker','info','--format','{{json .SecurityOptions}}'],text=True))
user='0:0' if any('rootless' in s for s in security) else str(os.getuid())+':'+str(os.getgid())
name='swarm-node-source';probe=subprocess.run(['docker','inspect',name],capture_output=True,text=True)
current=json.loads(probe.stdout)[0] if probe.returncode==0 else None
if current:
 labels=current['Config'].get('Labels') or {}
 if labels.get('organization.managed-by')!='run.py' or labels.get('org.lifetech.service')!='node-source':raise ValueError('Source container name belongs to another owner')
 if labels.get('org.lifetech.protected-source')=='true' and 'authorization' not in policy:raise ValueError('Refusing to remove source authorization during routine installation')
 if labels.get('org.lifetech.encrypted-source')=='true' and 'encrypted_catalogue' not in policy:raise ValueError('Refusing to remove encrypted source mode during routine installation')
 if labels.get('org.lifetech.revision')!=revision or current['Image']!=image:
  subprocess.run(['docker','rm','-f',name],check=True,capture_output=True);current=None
if not current:
 argv=['docker','run','-d','--name',name,'--restart','unless-stopped','--network','host','--read-only','--user',user,'--cap-drop','ALL','--security-opt','no-new-privileges','--memory','256m','--memory-swap','256m','--cpus','1','--pids-limit','64','--env','PYTHONDONTWRITEBYTECODE=1','--env','HOME='+str(home),'--label','organization.managed-by=run.py','--label','org.lifetech.service=node-source','--label','org.lifetech.revision='+revision,'--log-opt','max-size=1m','--log-opt','max-file=2']
 if 'authorization' in policy:argv+=['--label','org.lifetech.protected-source=true']
 if 'encrypted_catalogue' in policy:argv+=['--label','org.lifetech.encrypted-source=true']
 mounts={str(base):True,'/etc/machine-id':True,**{path:True for path in inputs},**{path:False for path in outputs},str(base/'objects'):False,str(base/'inboxes'):False}
 for path,readonly in mounts.items():argv+=['--mount','type=bind,src='+path+',dst='+path+(',readonly' if readonly else '')]
 health="import ssl,http.client; c=ssl.create_default_context(cafile="+repr(str(base/'ca.crt'))+"); c.load_cert_chain("+repr(str(base/'identity.crt'))+","+repr(str(base/'identity.key'))+"); h=http.client.HTTPSConnection("+repr(config['address'])+","+str(policy['port'])+",context=c,timeout=3); h.request('GET','/health'); r=h.getresponse(); assert r.status==200; r.read(); h.close()"
 import shlex
 argv+=['--health-cmd',shlex.join(['python3','-c',health]),'--health-interval','10s','--health-start-period','10s','--health-timeout','5s','--health-retries','3','--entrypoint','python3',image,str(release/'source.py'),'--base',str(base),'--policy',str(release/'policy.json')]
 subprocess.run(argv,check=True,capture_output=True)
elif not current['State']['Running']:subprocess.run(['docker','start',name],check=True,capture_output=True)
print(json.dumps({'node':config['name'],'machine':config['machine'],'container':name,'revision':revision,'port':policy['port'],'inputs':inputs}))
'''


def install(host,inputs,outputs,authorization=None,encrypted_catalogue=None):
    if encrypted_catalogue and authorization is None:
        raise ValueError('Encrypted catalogue requires source authorization')
    import sys
    root=Path(__file__).resolve().parents[2]
    sys.path.insert(0,str(root/'swarm-control/server'))
    from agent_client import CONFIG
    from agent_updates import LauncherUpdates
    folder=Path(__file__).resolve().parent
    cfg=json.loads(CONFIG.read_text())
    peer=cfg['peers'][host['host']]
    host={**host,'lan_address':host.get('lan_address') or (peer['address'] if isinstance(peer,dict) else peer)}
    payload={'node':host['host'],'machine':host['machine'],'controller':cfg['name'],'inputs':inputs,'outputs':outputs,
             'authorization':authorization,'encrypted_catalogue':encrypted_catalogue,
             'files':{name:(folder/source).read_text() for name,source in {
                 'source.py':'swarm_source_service.py','bulk.py':'swarm_bulk.py','transfer.py':'swarm_tls_transfer.py',
                 'authorization.py':'swarm_data_authorization.py','protected.py':'swarm_protected_sessions.py',
                 'contracts.py':'swarm_protected_contract.py','crypto.py':'swarm_crypt4gh.py'}.items()}}
    # Installation is administrative only. Steady-state source operations never
    # use SSH, command pipes, Docker exec, or another launcher.
    deployer=LauncherUpdates.__new__(LauncherUpdates)
    result=subprocess.run(deployer.command(host,INSTALL),input=json.dumps(payload),capture_output=True,text=True,timeout=90)
    if result.returncode:raise RuntimeError('Source service installation failed: '+result.stderr[-1200:])
    return json.loads(result.stdout)


if __name__=='__main__':
    import argparse
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--inventory',type=Path,required=True)
    parser.add_argument('--host',required=True)
    parser.add_argument('--input',action='append',required=True)
    parser.add_argument('--output',action='append',required=True)
    parser.add_argument('--authorization',type=Path,help='Protected source configuration JSON (remote secret/approval paths; no tokens)')
    parser.add_argument('--encrypted-catalogue',help='Source-host encrypted dataset registry path')
    args=parser.parse_args()
    host=next(row for row in json.loads(args.inventory.read_text()) if row['host']==args.host)
    if args.encrypted_catalogue and not args.authorization:parser.error('--encrypted-catalogue requires --authorization')
    print(json.dumps(install(host,args.input,args.output,json.loads(args.authorization.read_text()) if args.authorization else None,args.encrypted_catalogue)))
