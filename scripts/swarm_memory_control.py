"""Manage a native node memory service; Python only refreshes discovery metadata.

No page payload passes through this process. Route hints are short lived, retain
IPv6 scopes, and never replace the C++ service's enrolled mTLS authentication.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from scripts.swarm_immutable_read import service_candidates


def routes(rows):
    result=[]
    for family,kind,protocol,_,address in rows:
        if family not in (socket.AF_INET,socket.AF_INET6):continue
        row={'family':6 if family==socket.AF_INET6 else 4,'address':address[0].split('%')[0],
             'scope':address[3] if family==socket.AF_INET6 else 0}
        if row not in result:result.append(row)
    # Interleave families before the native connector's bounded address race.
    v6=[r for r in result if r['family']==6];v4=[r for r in result if r['family']==4]
    return [group[i] for i in range(max(len(v6),len(v4))) for group in (v6,v4) if i<len(group)]


def supervise(config_path,binary):
    config=json.loads(config_path.read_text());enrollment=json.loads((Path(config['tls_root'])/'config.json').read_text())
    names={name for obj in config['objects'] for name in [*obj['peers'],*(s['source'] for s in obj['sources'])]}
    if any(name not in enrollment['peers'] for name in names):raise ValueError('Unenrolled memory route')
    pending={};answers={};due={};stopped=False;process=None
    def stop(*_):
        nonlocal stopped
        stopped=True
        if process is not None and process.poll() is None:process.terminate()
    signal.signal(signal.SIGTERM,stop);signal.signal(signal.SIGINT,stop)
    pools={'nss':ThreadPoolExecutor(max_workers=4),'dns-sd':ThreadPoolExecutor(max_workers=4)}
    started=time.monotonic();snapshot=Path(config['discovery'])
    try:
        while not stopped:
            now=time.monotonic()
            for name in names:
                for kind in pools:
                    key=(name,kind)
                    if key in pending and pending[key].done():
                        try:answers[key]=(now,routes(pending[key].result()))
                        except (OSError,ValueError):answers[key]=(now,[])
                        del pending[key];due[key]=now+3
                    if key not in pending and due.get(key,0)<=now:
                        if kind=='nss':pending[key]=pools[kind].submit(socket.getaddrinfo,name,enrollment['port'],socket.AF_UNSPEC,socket.SOCK_STREAM)
                        else:pending[key]=pools[kind].submit(service_candidates,name,enrollment['port'],1)
            hosts={name:[] for name in names}
            for (name,_),(when,rows) in answers.items():
                if now-when<15:
                    for row in rows:
                        if row not in hosts[name]:hosts[name].append(row)
            snapshot.parent.mkdir(parents=True,exist_ok=True)
            temporary=snapshot.with_suffix('.tmp')
            temporary.write_text(json.dumps({'updated':int(time.time()),'hosts':hosts}));temporary.chmod(0o600);temporary.replace(snapshot)
            if process is None and (all(hosts.values()) or now-started>=3):
                process=subprocess.Popen([str(binary),'--config',str(config_path)])
            if process is not None and process.poll() is not None:return process.returncode
            time.sleep(.25)
    finally:
        stop()
        if process is not None:
            try:process.wait(timeout=20)
            except subprocess.TimeoutExpired:process.kill();process.wait()
        for pool in pools.values():pool.shutdown(wait=False,cancel_futures=True)
    return 0


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',type=Path,required=True);parser.add_argument('--binary',type=Path,required=True)
    args=parser.parse_args();raise SystemExit(supervise(args.config,args.binary))
