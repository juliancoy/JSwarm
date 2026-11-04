"""Local subprocess adapter to authenticated launcher owner command pipes; no SSH."""
import argparse
import base64
import http.client
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


def run(host,argv):
    client=AgentClient(host);ident=uuid.uuid4().hex;started=False
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
        call('command.start',{'id':ident,'argv':argv});started=True
        inputs=queue.Queue(maxsize=2)
        def read_input():
            while True:
                block=sys.stdin.buffer.read(256*1024);inputs.put(block)
                if not block:return
        threading.Thread(target=read_input,daemon=True).start()
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
        if started:
            try:call('command.close',{'id':ident})
            except Exception:pass
        client.close()


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--host',required=True);parser.add_argument('--endpoint',required=True);parser.add_argument('--machine',required=True);parser.add_argument('argv',nargs=argparse.REMAINDER);args=parser.parse_args()
    signal.signal(signal.SIGTERM,lambda *_:sys.exit(143))
    try:raise SystemExit(run({'host':args.host,'agent_endpoint':args.endpoint,'machine':args.machine},args.argv[1:] if args.argv[:1]==['--'] else args.argv))
    except BrokenPipeError:raise SystemExit(1)
    except Exception as error:print(type(error).__name__+': '+str(error),file=sys.stderr);raise SystemExit(1)
