"""Publish the inspector log endpoint alongside the existing API bridge."""
import hashlib
import json
from pathlib import Path
import shlex
import subprocess


def deploy():
    workspace=Path(__file__).resolve().parents[2]
    source=workspace/'JSwarm/swarm-control/server'
    units=('swarm-tasks.service','swarm-control-api.service')
    commands={}
    for unit in units:
        raw=subprocess.check_output(['systemctl','--user','show',unit,'-p','ExecStart','--value'],text=True)
        argv=shlex.split(raw.split('argv[]=',1)[1].split(' ;',1)[0]);commands[unit]=argv
    additions=r'''
if name=='task_dashboard.py' and '/api/tasks/logs' not in text:
 text=text.replace("'/api/tasks/pipeline', '/api/tasks/machine')","'/api/tasks/pipeline', '/api/tasks/machine', '/api/tasks/logs')")
 marker="            if path.path == '/api/tasks/machine':"
 assert marker in text
 text=text.replace(marker,"            if path.path == '/api/tasks/logs':\n                from shard_logs import tail\n                try:data=tail(parse_qs(path.query).get('id',[''])[0],snapshot=lambda: pipeline_feed.sample or {'items':[]})\n                except ValueError:\n                    self.send_error(404,'Unknown shard');return\n            elif path.path == '/api/tasks/machine':",1)
if name=='api.py' and '/api/tasks/logs' not in text:
 text=text.replace("'/api/tasks/pipeline','/api/tasks/machine'","'/api/tasks/pipeline','/api/tasks/logs','/api/tasks/machine'")
 marker="                if path == '/api/tasks/machine':"
 assert marker in text
 text=text.replace(marker,"                if path == '/api/tasks/logs':\n                    ident=parse_qs(urlsplit(self.path).query).get('id',[''])[0]\n                    if not ident or len(ident)>2048:raise ValueError('Invalid shard')\n                    target += '?' + urlencode({'id':ident})\n                elif path == '/api/tasks/machine':",1)
sys.path.insert(0,str(pathlib.Path(__file__).resolve().parent))
'''
    files={'shard_logs.py':(source/'shard_logs.py').read_bytes()}
    for unit,argv in commands.items():
        bridge=Path(argv[1]);code=bridge.read_text()
        if not any(part in str(bridge) for part in ('genome-explorer-runtime','pipeline-terminal')):raise RuntimeError('Expected existing explorer bridge; preserve unknown deployment')
        code=code.replace('overlay=pathlib.Path(__file__).resolve().parent','overlay=pathlib.Path('+repr(str(bridge.parent))+')')
        marker="namespace={'__name__':'__main__'"
        assert marker in code
        if "if name=='task_dashboard.py' and '/api/tasks/logs'" not in code:code=code.replace(marker,additions+'\n'+marker)
        else:code=code.replace("tail(parse_qs(path.query).get('id',[''])[0])","tail(parse_qs(path.query).get('id',[''])[0],snapshot=lambda: pipeline_feed.sample or {'items':[]})")
        files[unit+'.py']=code.encode()
    revision=hashlib.sha256(b''.join(name.encode()+data for name,data in sorted(files.items()))).hexdigest()
    release=Path.home()/'.local/state/swarm-control/pipeline-terminal'/revision;release.mkdir(parents=True,exist_ok=True)
    for name,data in files.items():
        compile(data,name,'exec');path=release/name
        if path.exists() and path.read_bytes()!=data:raise ValueError('Immutable release differs')
        if not path.exists():path.write_bytes(data);path.chmod(0o400)
    for unit,argv in commands.items():
        drop=Path.home()/'.config/systemd/user'/(unit+'.d');drop.mkdir(parents=True,exist_ok=True)
        (drop/'terminal.conf').unlink(missing_ok=True)
        (drop/'zzz-terminal.conf').write_text('[Service]\nExecStart=\nExecStart='+shlex.join([argv[0],str(release/(unit+'.py')),argv[-1]])+'\n')
    subprocess.run(['systemctl','--user','daemon-reload'],check=True)
    subprocess.run(['systemctl','--user','restart',*units],check=True)
    subprocess.run(['systemctl','--user','is-active','--quiet',*units],check=True)
    print(json.dumps({'release':str(release),'revision':revision}))

if __name__=='__main__':deploy()
