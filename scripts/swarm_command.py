"""Local subprocess adapter to authenticated launcher owner command pipes; no SSH."""
import argparse
import base64
import http.client
import hashlib
import json
import re
import socket
import subprocess
import os
import queue
import threading
from pathlib import Path
import select
import signal
import sys
import time
import uuid

ROOT=(Path(__file__).resolve().parents[2] if Path(__file__).resolve().parents[1].name in ('GenomeHarbor', 'JSwarm') else Path(__file__).resolve().parents[1])
sys.path.insert(0,str(ROOT/'swarm-control/server'))
from agent_client import AgentClient


def process_start(pid):
    try:
        value=Path('/proc',str(pid),'stat').read_text()
        return value[value.rfind(')')+2:].split()[19]
    except (FileNotFoundError,ProcessLookupError):return None


def journal_root():
    return Path(os.environ.get('SWARM_SUBMISSION_QUEUE',str(Path.home()/'.local/state/swarm-control/submission/queue.sqlite'))).parent/'command-clients'


def caller_record(host,ident,argv):
    namespace=os.readlink('/proc/self/ns/pid')
    name=socket.gethostname()
    return {'id':ident,'host':host['host'],'machine':host['machine'],'agent_endpoint':host['agent_endpoint'],'agent_route':host.get('agent_route'),'pid':os.getpid(),
        'start':process_start(os.getpid()),'namespace':namespace,
        'boot':Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
        'container':name if Path('/.dockerenv').exists() and re.fullmatch('[0-9a-f]{12,64}',name) else None,
        'argv_sha256':hashlib.sha256(json.dumps(argv,separators=(',',':')).encode()).hexdigest()}


def caller_gone(record):
    if record['boot']!=Path('/proc/sys/kernel/random/boot_id').read_text().strip():return True
    if record['namespace']==os.readlink('/proc/self/ns/pid'):
        return process_start(record['pid'])!=record['start']
    # Never mistake a PID in another namespace for a dead host process. A
    # stopped/removed containing Docker instance is positive owner-death proof.
    container=record.get('container')
    if not container:return False
    result=subprocess.run(['docker','inspect','--format','{{json .State.Running}}',container],capture_output=True,text=True,timeout=5)
    if result.returncode==0:return json.loads(result.stdout) is False
    return 'no such object' in result.stderr.lower() or 'no such container' in result.stderr.lower()


def reap_orphan_clients(root=None,factory=None,*,host=None):
    factory=factory or AgentClient
    root=Path(root) if root is not None else journal_root()
    if not root.exists():return 0
    closed=0
    for path in root.glob('*.json'):
        client=None
        try:
            if path.is_symlink() or path.stat().st_size>8192:continue
            record=json.loads(path.read_text())
            if host is not None and record['host']!=host:continue
            if not re.fullmatch('[0-9a-f]{32}',record['id']) or not caller_gone(record):continue
            recovered={'host':record['host'],'machine':record['machine'],'agent_endpoint':record['agent_endpoint']}
            if record.get('agent_route') in ('auto','webrtc'):recovered['agent_route']=record['agent_route']
            client=factory(recovered)
            if client.call('status',timeout=5).get('machine')!=record['machine']:continue
            if client.call('command.close',{'id':record['id']},timeout=10).get('closed') is True:
                path.unlink();closed+=1
        except (OSError,RuntimeError,ValueError,KeyError,subprocess.SubprocessError):pass
        finally:
            if client is not None:client.close()
    return closed


def save_caller(path,record):
    path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    temp=path.with_suffix('.tmp')
    with temp.open('w') as stream:
        json.dump(record,stream);stream.flush();os.fsync(stream.fileno())
    temp.chmod(0o600);temp.replace(path)


def input_reader(fd, inputs, stopped):
    """Read cancellably without holding Python's buffered stdin lock at exit."""
    while not stopped.is_set():
        if not select.select([fd], [], [], .1)[0]:continue
        block=os.read(fd,256*1024)
        while not stopped.is_set():
            try:inputs.put(block,timeout=.1);break
            except queue.Full:continue
        if not block:return


def run(host,argv):
    # An offline peer must not delay commands on a healthy worker. Its
    # retained journals are reconciled when that peer is contacted again.
    reap_orphan_clients(host=host['host'])
    client=AgentClient(host);ident=uuid.uuid4().hex;started=False;start_requested=False
    journal=journal_root()/(ident+'.json')
    input_stopped=threading.Event();reader=None
    save_caller(journal,caller_record(host,ident,argv))
    def call(op,args):
        deadline=time.monotonic()+180
        while True:
            try:return client.call(op,args,timeout=30)
            except (OSError,http.client.HTTPException):
                if time.monotonic()>=deadline:raise
                client.close();time.sleep(.2)
    try:
        status=call('status',{})
        if status['machine']!=host['machine']:raise ValueError('Enrolled command machine identity changed')
        if 'node-command-v1' not in status.get('capabilities',[]):raise RuntimeError('Launcher command transport unavailable; update launcher; SSH fallback is disabled')
        start_requested=True
        call('command.start',{'id':ident,'argv':argv});started=True
        inputs=queue.Queue(maxsize=2)
        reader=threading.Thread(target=input_reader,args=(sys.stdin.fileno(),inputs,input_stopped),daemon=True)
        reader.start()
        offsets={'stdout':0,'stderr':0};input_offset=0;credit=512*1024;eof=False;pending=b'' 
        while True:
            block=b''
            if not eof and credit:
                if not pending:
                    try:
                        pending=inputs.get_nowait()
                        if not pending:eof=True
                    except queue.Empty:pass
                block,pending=pending[:credit],pending[credit:]
            args={'id':ident,**offsets,'offset':input_offset,'data':base64.b64encode(block).decode(),'eof':eof}
            row=call('command.poll',args)
            if row['input_offset']!=input_offset+len(block):raise ValueError('Command stdin acknowledgement differs')
            input_offset=row['input_offset'];credit=row['input_credit']
            output=False
            for name in offsets:
                if row[name+'_offset']!=offsets[name]:raise ValueError('Command output cursor differs')
                data=base64.b64decode(row[name],validate=True)
                if data:
                    target=getattr(sys,name).buffer;target.write(data);target.flush();offsets[name]+=len(data);output=True
            if row['exit_code'] is not None and row['stdout_eof'] and row['stderr_eof']:return row['exit_code']
            if not block and not output:time.sleep(.05)
    finally:
        input_stopped.set()
        if reader is not None:reader.join(timeout=1)
        closed=not start_requested
        if start_requested:
            try:closed=call('command.close',{'id':ident}).get('closed') is True and started
            except Exception:pass
        if closed:journal.unlink(missing_ok=True)
        client.close()


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--host',required=True);parser.add_argument('--endpoint',required=True);parser.add_argument('--machine',required=True);parser.add_argument('--route',choices=('auto','webrtc'));parser.add_argument('argv',nargs=argparse.REMAINDER);args=parser.parse_args()
    signal.signal(signal.SIGTERM,lambda *_:sys.exit(143))
    try:raise SystemExit(run({'host':args.host,'agent_endpoint':args.endpoint,'machine':args.machine,'agent_route':args.route},args.argv[1:] if args.argv[:1]==['--'] else args.argv))
    except BrokenPipeError:raise SystemExit(1)
    except Exception as error:print(type(error).__name__+': '+str(error),file=sys.stderr);raise SystemExit(1)
