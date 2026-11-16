"""Install the native immutable-memory data service through enrolled owner grants.

New mounts are versioned. Existing mounts and accepted jobs are left running;
only independently verified records should replace a scheduler's deployment.
"""
import argparse
import base64
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path

from scripts.deploy_swarm_memory import PROJECT, execute
from scripts.swarm_memory import validate

FILES = ('scripts/__init__.py', 'scripts/swarm_memory_control.py',
         'scripts/swarm_immutable_read.py', 'scripts/launcher_discovery.py',
         'scripts/swarm_host_service.py')

INSTALL = r'''
import base64,hashlib,json,os,pathlib,shlex,sys
p=json.load(sys.stdin);os.umask(0o077)
if hashlib.sha256(pathlib.Path('/etc/machine-id').read_bytes()).hexdigest()!=p['machine']:raise ValueError('Memory client machine differs')
home=pathlib.Path.home();settings=json.loads((home/'.config/lifetech-launcher/filesystem.json').read_text())
enrolled=json.loads((home/'.config/lifetech-launcher/peers.json').read_text())
if enrolled['name']!=p['node']:raise ValueError('Memory client name differs')
if any(n not in enrolled['peers'] for n in p['clients']):raise ValueError('Unenrolled memory participant')
binary=base64.b64decode(p['binary'],validate=True)
if hashlib.sha256(binary).hexdigest()!=p['binary_sha256']:raise ValueError('Native binary differs')
version=p['revision'];root=home/'.local/share/swarm-memory-native'/version;root.mkdir(parents=True,exist_ok=True)
for name,data in {**{n:s.encode() for n,s in p['files'].items()},**{'dependencies/'+n:base64.b64decode(s,validate=True) for n,s in p['wheels'].items()},**{'lib/'+n:base64.b64decode(s,validate=True) for n,s in p['libraries'].items()},'bin/swarm-memory':binary}.items():
 target=root/name;target.parent.mkdir(parents=True,exist_ok=True)
 if target.exists() and target.read_bytes()!=data:raise ValueError('Immutable native runtime changed')
 if not target.exists():target.write_bytes(data);target.chmod(0o500 if name.startswith('bin/') else 0o400)
mount=home/'SwarmMemory'/p['object']['id']/version;mount.mkdir(parents=True,exist_ok=True)
state=home/'.local/state/swarm-memory-native'/version;state.mkdir(parents=True,exist_ok=True)
config={'tls_root':str(home/'.local/state/lifetech-launcher/bulk-transport'),'discovery':str(state/'discovery.json'),
        'cache_bytes':p['cache_bytes'],'replicas':p['replicas'],'peer_port':p['peer_port'],
        'objects':[{'manifest':p['object'],'sources':p['sources'],'mountpoint':str(mount),
                    'peers':[n for n in p['clients'] if n!=p['node']],'allowed':p['clients'],
                    'allow_other':settings.get('allow_other',False),'allow_root':settings.get('allow_root',True)}]}
config_text=json.dumps(config,sort_keys=True);target=root/'config.json'
if target.exists() and target.read_text()!=config_text:raise ValueError('Immutable native configuration changed')
if not target.exists():target.write_text(config_text);target.chmod(0o400)
unit='swarm-memory-native-'+version[:24]+'.service'
units=home/'.config/systemd/user';units.mkdir(parents=True,exist_ok=True)
command=shlex.join(['/usr/bin/python3',str(root/'scripts/swarm_memory_control.py'),'--config',str(target),'--binary',str(root/'bin/swarm-memory')])
deps=':'.join(str(root/'dependencies'/name) for name in sorted(p['wheels']))
text='[Unit]\nDescription=Native shared immutable swarm memory\nAfter=network-online.target\n[Service]\nExecStart='+command+'\nEnvironment="PYTHONPATH='+str(deps)+'"\nEnvironment=PYTHONDONTWRITEBYTECODE=1\nEnvironment=LD_LIBRARY_PATH='+str(root/'lib')+'\nUMask=0077\nRestart=on-failure\nRestartSec=5\nTimeoutStopSec=25\nStandardOutput=append:'+str(state/'service.log')+'\nStandardError=inherit\nMemoryHigh=384M\n[Install]\nWantedBy=default.target\n'
target=units/unit
if target.exists() and target.read_text()!=text:raise ValueError('Immutable native service changed')
if not target.exists():target.write_text(text)
namespace={};exec(p['files']['scripts/swarm_host_service.py'],namespace)
systemctl=namespace['systemctl'];systemctl('daemon-reload',check=True,capture_output=True)
systemctl('enable',unit,check=True,capture_output=True);systemctl('start',unit,check=True,capture_output=True)
print(json.dumps({'node':p['node'],'machine':p['machine'],'object_id':p['object']['id'],'revision':version,
                  'path':str(mount/'object'),'metrics':str(mount/'metrics.json'),'unit':unit,'backend':'native-cpp.v1',
                  'binary_sha256':p['binary_sha256'],'peer_port':p['peer_port'],'release':str(root),'ready':False}))
'''


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--publication',type=Path,required=True)
    parser.add_argument('--binary',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--python-dependency',type=Path,action='append',required=True,help='Pure Python wheels resolved for the oldest fleet Python (3.10)')
    parser.add_argument('--node',action='append',required=True)
    parser.add_argument('--install-node',action='append',help='Install only these participants, preserving existing deployment records for the others')
    parser.add_argument('--runtime-library',type=Path,action='append',default=[],help='Optional portable libfuse3.so.3 with adjacent .copyright file')
    parser.add_argument('--cache-mib',type=int,default=256)
    parser.add_argument('--replicas',type=int,default=1)
    parser.add_argument('--port',type=int,default=9450)
    args=parser.parse_args()
    if not 4<=args.cache_mib<=1024 or not 0<=args.replicas<=4 or not 1024<=args.port<=65535:raise ValueError('Invalid service limits')
    publication=json.loads(args.publication.read_text());validate(publication['object'])
    clients=sorted(set(args.node))
    selected=sorted(set(args.install_node or clients))
    if any(n not in clients for n in selected):raise ValueError('Installation target is not a participant')
    if any(name not in source['clients'] for source in publication['sources'] for name in clients):raise ValueError('Publish source grants for every participant first')
    binary=args.binary.read_bytes()
    import zipfile
    wheels={}
    for path in args.python_dependency:
        with zipfile.ZipFile(path) as archive:
            metadata=[n for n in archive.namelist() if n.endswith('.dist-info/WHEEL')]
            if len(metadata)!=1 or b'Root-Is-Purelib: true' not in archive.read(metadata[0]):raise ValueError('Only pure Python wheels are portable')
        data=path.read_bytes();wheels[hashlib.sha256(data).hexdigest()+'.whl']=base64.b64encode(data).decode()
    libraries={}
    for path in args.runtime_library:
        if path.name!='libfuse3.so.3' or not path.read_bytes().startswith(b'\x7fELF'):raise ValueError('Expected portable libfuse3.so.3')
        for item in [path,path.with_name(path.name+'.copyright')]:libraries[item.name]=base64.b64encode(item.read_bytes()).decode()
    payload={'libraries':libraries,'wheels':wheels,'installer_sha256':hashlib.sha256(INSTALL.encode()).hexdigest(),'files':{name:(PROJECT/name).read_text() for name in FILES},'binary_sha256':hashlib.sha256(binary).hexdigest(),
             'object':publication['object'],'sources':publication['sources'],'clients':clients,
             'cache_bytes':args.cache_mib*1024*1024,'replicas':args.replicas,'peer_port':args.port}
    payload['revision']=hashlib.sha256(json.dumps(payload,sort_keys=True).encode()).hexdigest()
    payload['binary']=base64.b64encode(binary).decode()
    def install(name):
        try:return name,execute(name,INSTALL,payload,timeout=120)
        except Exception as error:return name,{'node':name,'ready':False,'error':str(error)}
    nodes={}
    if args.install_node and args.output.exists():
        previous=json.loads(args.output.read_text())
        if previous['object_id']!=publication['object']['id']:raise ValueError('Existing deployment object differs')
        nodes=previous['nodes']
    with ThreadPoolExecutor(max_workers=5) as pool:nodes.update(dict(pool.map(install,selected)))
    result={'publication':str(args.publication.resolve()),'object_id':publication['object']['id'],'nodes':nodes}
    args.output.parent.mkdir(parents=True,exist_ok=True)
    temporary=args.output.with_suffix('.tmp');temporary.write_text(json.dumps(result,indent=2));temporary.replace(args.output)
    print(json.dumps(result,indent=2))
    return int(any('error' in nodes[name] for name in selected))

if __name__=='__main__':raise SystemExit(main())
