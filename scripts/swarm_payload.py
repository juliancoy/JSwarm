"""Direct pinned TLS payloads; only setup and completion use signed node commands.

Streams feed existing RAM cache loaders and verified origin publishers. No SSH,
new cache, payload staging, or persistent listener is involved.
"""
import argparse
import json
import os
from pathlib import Path
import re
import secrets
import selectors
import subprocess
import sys

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from scripts import swarm_runtime as runtime
from scripts.swarm_cache_transfer import certificate

NETWORK=r'''
import errno,json,socket,struct
BLOCK=256*1024

def transport_socket(family):
 try:return socket.socket(family,socket.SOCK_STREAM,getattr(socket,'IPPROTO_MPTCP',262))
 except OSError as error:
  if error.errno not in (errno.EPROTONOSUPPORT,errno.ENOPROTOOPT,errno.EINVAL,errno.EAFNOSUPPORT):raise
  return socket.socket(family,socket.SOCK_STREAM)

def exact(connection,n):
 result=bytearray()
 while len(result)<n:
  part=connection.recv(n-len(result))
  if not part:raise ConnectionError('Truncated TLS payload')
  result.extend(part)
 return bytes(result)

def send(connection,data):
 if len(data)>BLOCK:raise ValueError('Payload frame exceeds bound')
 connection.sendall(struct.pack('!I',len(data))+data)

def receive(connection):
 n=struct.unpack('!I',exact(connection,4))[0]
 if n>BLOCK:raise ValueError('Payload frame exceeds bound')
 return exact(connection,n)

def connect(name,port,addresses=()):
 errors=[];seen=set()
 for target in [name,*addresses]:
  try:resolved=socket.getaddrinfo(target,port,socket.AF_UNSPEC,socket.SOCK_STREAM)
  except OSError as error:errors.append(str(error));continue
  for family,kind,protocol,canonical,address in resolved:
   if (family,address) in seen:continue
   seen.add((family,address));raw=None
   try:
    raw=transport_socket(family);raw.settimeout(3);raw.connect(address);return raw
   except OSError as error:
    errors.append(str(error))
    if raw is not None:raw.close()
 raise ConnectionError('No reachable enrolled payload address: '+'; '.join(errors)[-500:])

def listen():
 for family,address in ((socket.AF_INET6,('::',0)),(socket.AF_INET,('0.0.0.0',0))):
  server=None
  try:
   server=transport_socket(family)
   if family==socket.AF_INET6:server.setsockopt(socket.IPPROTO_IPV6,socket.IPV6_V6ONLY,0)
   server.bind(address);server.listen(4);server.settimeout(45);return server
  except OSError:
   if server is not None:server.close()
 raise ConnectionError('No payload listener address family available')
'''
exec(NETWORK)

REMOTE=NETWORK+r'''
import hashlib,hmac,pathlib,ssl,tempfile,time,sys
p=json.load(sys.stdin)
if hashlib.sha256(pathlib.Path('/etc/machine-id').read_bytes()).hexdigest()!=p['machine']:raise ValueError('Payload machine identity changed')
mounts=pathlib.Path('/proc/self/mountinfo').read_text().splitlines()
if not any(v[4]=='/tmp' and v[v.index('-')+1] in ('tmpfs','ramfs') for line in mounts if (v:=line.split())):raise ValueError('TLS scratch must be RAM-backed')
with tempfile.TemporaryDirectory(dir='/tmp',prefix='payload-tls-') as directory:
 root=pathlib.Path(directory);(root/'cert').write_text(p['certificate']);(root/'key').write_text(p['private_key']);(root/'key').chmod(0o600)
 context=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER);context.minimum_version=ssl.TLSVersion.TLSv1_2;context.load_cert_chain(root/'cert',root/'key')
 with listen() as server:
  print(json.dumps({'port':server.getsockname()[1]}),flush=True)
  deadline=time.monotonic()+45;authenticated=False
  while not authenticated and time.monotonic()<deadline:
   raw,_=server.accept();raw.settimeout(5)
   try:
    connection=context.wrap_socket(raw,server_side=True)
    if not hmac.compare_digest(exact(connection,64),p['token'].encode()):connection.close();continue
    authenticated=True
   except (ssl.SSLError,OSError):raw.close()
  if not authenticated:raise TimeoutError('Payload peer did not authenticate')
  with connection, tempfile.TemporaryFile(dir='/tmp') as errors:
   connection.settimeout(60)
   import subprocess
   process=subprocess.Popen(p['command'],stdin=subprocess.PIPE if p['direction']=='upload' else subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=errors)
   try:
    if p['direction']=='upload':
     while block:=receive(connection):process.stdin.write(block)
     process.stdin.close();process.stdin=None
     output=process.stdout.read(BLOCK+1)
     if len(output)>BLOCK:raise ValueError('Payload receiver response exceeds bound')
     process.wait(timeout=60)
    else:
     while block:=process.stdout.read(BLOCK):send(connection,block)
     send(connection,b'');output=b'';process.wait(timeout=60)
    errors.seek(0);message=errors.read(8192).decode(errors='replace')
    send(connection,json.dumps({'returncode':process.returncode,'stdout':output.decode(),'stderr':message}).encode())
   finally:
    if process.poll() is None:process.kill();process.wait()
    if process.stdin is not None:process.stdin.close()
    process.stdout.close()
 print(json.dumps({'complete':True}),flush=True)
'''


def command(host,argv,*,direction):
    if direction not in ('upload','download'):raise ValueError('Invalid payload direction')
    if host.get('local'):return argv
    if not host.get('agent_endpoint') or not re.fullmatch('[a-f0-9]{64}',host.get('machine','')):
        raise ValueError('Enrolled payload endpoint and machine required')
    return [sys.executable,str(Path(__file__).resolve()),'--host',json.dumps(host),
            '--direction',direction,'--',*argv]


def stream(host,argv,direction,source,sink):
    import ssl
    settings=json.loads(runtime.CONFIG.read_text());name=host['host']
    if name not in settings['peers'] or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9.-]*',name):
        raise ValueError('Payload requires an enrolled hostname')
    cert,key=certificate(name);token=secrets.token_hex(32)
    setup={'machine':host['machine'],'command':argv,'direction':direction,
           'certificate':cert,'private_key':key,'token':token}
    owner=subprocess.Popen(runtime.shell_command({**host,'local':False},['python3','-c',REMOTE]),
                           stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
    try:
        owner.stdin.write(json.dumps(setup));owner.stdin.close();owner.stdin=None
        with selectors.DefaultSelector() as selector:
            selector.register(owner.stdout,selectors.EVENT_READ)
            if not selector.select(40):raise TimeoutError('Payload owner did not become ready')
        announcement=owner.stdout.readline()
        if not announcement:raise RuntimeError('Payload owner failed: '+owner.stderr.read()[-2000:])
        port=json.loads(announcement)['port']
        if type(port) is not int or not 1024<=port<=65535:raise ValueError('Invalid payload port')
        context=ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT);context.minimum_version=ssl.TLSVersion.TLSv1_2
        context.load_verify_locations(cadata=cert)
        with connect(name,port,host.get('resolved_addresses',[])) as raw:
            with context.wrap_socket(raw,server_hostname=name) as connection:
                connection.settimeout(60);connection.sendall(token.encode())
                if direction=='upload':
                    while block:=source.read(BLOCK):send(connection,block)
                    send(connection,b'')
                else:
                    while block:=receive(connection):sink.write(block)
                result=json.loads(receive(connection))
                if result['returncode']:raise RuntimeError('Payload command failed: '+result['stderr'])
                if direction=='upload':sink.write(result['stdout'].encode())
        output,errors=owner.communicate(timeout=30)
        if owner.returncode or json.loads(output)!={'complete':True}:raise RuntimeError('Payload owner failed: '+errors[-2000:])
    finally:
        if owner.poll() is None:
            owner.terminate()
            try:owner.wait(timeout=5)
            except subprocess.TimeoutExpired:owner.kill();owner.wait()
        for pipe in (owner.stdin,owner.stdout,owner.stderr):
            if pipe is not None:pipe.close()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host',type=json.loads,required=True)
    parser.add_argument('--direction',choices=('upload','download'),required=True)
    parser.add_argument('argv',nargs=argparse.REMAINDER)
    args=parser.parse_args();argv=args.argv[1:] if args.argv[:1]==['--'] else args.argv
    if not argv:parser.error('Payload command required')
    stream(args.host,argv,args.direction,sys.stdin.buffer,sys.stdout.buffer)

if __name__=='__main__':main()
