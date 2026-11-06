"""One-use, pinned TLS cache streams between enrolled command owners.

Only control messages pass through the coordinator. Data goes from the source
Docker cache directly to the destination Docker cache, without a payload file.
"""
import datetime
import ipaddress
import json
import secrets
import selectors
import subprocess
import time

from scripts.swarm_runtime import shell_command
from scripts.swarm_shared_cache import CHECK,contract


RUNTIME=r'''
import errno,hashlib,hmac,json,os,pathlib,socket,ssl,subprocess,sys,tempfile,time
p=json.load(sys.stdin)
if hashlib.sha256(pathlib.Path('/etc/machine-id').read_bytes()).hexdigest()!=p['machine']:raise ValueError('Cache transfer machine changed')
def transport_socket():
 try:return socket.socket(socket.AF_INET,socket.SOCK_STREAM,getattr(socket,'IPPROTO_MPTCP',262))
 except OSError as error:
  if error.errno not in (errno.EPROTONOSUPPORT,errno.ENOPROTOOPT,errno.EINVAL):raise
  return socket.socket(socket.AF_INET,socket.SOCK_STREAM)
cache=p['cache'];matches={}
sockets=[os.environ.get('DOCKER_HOST','unix:///var/run/docker.sock'),'unix:///var/run/docker.sock','unix:///run/user/1000/docker.sock']
for endpoint in dict.fromkeys(sockets):
 if endpoint.startswith('unix://') and not pathlib.Path(endpoint[7:]).exists():continue
 prefix=['docker','--host',endpoint]
 r=subprocess.run(prefix+['inspect',cache['container']],capture_output=True,text=True,timeout=10)
 if r.returncode:continue
 row=json.loads(r.stdout)[0]
 if (row['Name'].lstrip('/')!=cache['container'] or row['Image']!=cache['image'] or not row['State']['Running']
     or (row['Config'].get('Labels') or {}).get('org.lifetech.cache-owner')!=cache['key']):raise ValueError('Cache owner changed')
 matches[row['Id']]=(prefix,row)
if len(matches)!=1:raise ValueError('Unique cache engine unavailable')
prefix,row=next(iter(matches.values()))
def worker(argv):return prefix+['exec','-i',cache['container'],*argv]
if p['mode']=='send':
 expected={k:cache[k] for k in ('key','files')};expected.update(container_id=row['Id'],started_at=row['State']['StartedAt'])
 check=subprocess.run(worker(['python3','-c',p['check'],json.dumps(expected)]),capture_output=True,text=True,timeout=20)
 if check.returncode or not isinstance(json.loads(check.stdout),dict):raise ValueError('Source cache is no longer sealed')
 # Ephemeral TLS keys live only on the owner's bounded RAM scratch mount.
 mounts=pathlib.Path('/proc/self/mountinfo').read_text().splitlines()
 if not any(v[4]=='/tmp' and v[v.index('-')+1] in ('tmpfs','ramfs') for line in mounts if (v:=line.split())):raise ValueError('TLS scratch must be RAM-backed')
 with tempfile.TemporaryDirectory(dir='/tmp',prefix='cache-tls-') as temp:
  root=pathlib.Path(temp);(root/'cert').write_text(p['certificate']);(root/'key').write_text(p['private_key']);(root/'key').chmod(0o600)
  context=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER);context.minimum_version=ssl.TLSVersion.TLSv1_2;context.load_cert_chain(root/'cert',root/'key')
  with transport_socket() as server:
   server.bind((p['address'],0));server.listen(4);server.settimeout(30)
   print(json.dumps({'port':server.getsockname()[1]}),flush=True)
   deadline=time.monotonic()+30;accepted=False
   while time.monotonic()<deadline and not accepted:
    raw,_=server.accept();raw.settimeout(5)
    try:
     with context.wrap_socket(raw,server_side=True) as connection:
      token=b''
      while len(token)<64:
       part=connection.recv(64-len(token))
       if not part:break
       token+=part
      if not hmac.compare_digest(token,p['token'].encode()):continue
      accepted=True;connection.settimeout(20)
      process=subprocess.Popen(worker(['tar','-C','/dev/shm','-cf','-',*cache['files']]),stdout=subprocess.PIPE,stderr=subprocess.PIPE)
      try:
       while block:=process.stdout.read(256*1024):connection.sendall(block)
       if process.wait(timeout=10):raise RuntimeError('Cache source stream failed')
      finally:
       if process.poll() is None:process.kill();process.wait()
       process.stdout.close();process.stderr.close()
    except (ssl.SSLError,OSError):
     raw.close()
     if accepted:raise
   if not accepted:raise TimeoutError('Cache receiver did not authenticate')
else:
 context=ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT);context.minimum_version=ssl.TLSVersion.TLSv1_2
 context.load_verify_locations(cadata=p['certificate'])
 limit=sum(v['bytes'] for v in cache['files'].values())+10240+1024*len(cache['files']);total=0
 with transport_socket() as raw:
  raw.settimeout(10);raw.connect((p['address'],p['port']))
  with context.wrap_socket(raw,server_hostname=p['address']) as connection:
   connection.settimeout(20);connection.sendall(p['token'].encode())
   process=subprocess.Popen(worker(['python3','-c',p['receive'],'/dev/shm',json.dumps(cache['files'])]),stdin=subprocess.PIPE,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
   try:
    while block:=connection.recv(256*1024):
     total+=len(block)
     if total>limit:raise ValueError('Cache transfer exceeds immutable bounds')
     process.stdin.write(block)
    process.stdin.close()
    if process.wait(timeout=20):raise ValueError('Cache receiver rejected immutable contents')
   finally:
    if process.poll() is None:process.kill();process.wait()
    if not process.stdin.closed:process.stdin.close()
    process.stderr.close()
 print(json.dumps({'bytes':total,'transport':'direct-pinned-tls'}))
'''


def certificate(address):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes,serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID
    ip=ipaddress.ip_address(address)
    if not ip.is_private or ip.is_loopback:raise ValueError('Enrolled LAN source required')
    key=ec.generate_private_key(ec.SECP256R1());name=x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,'JSwarm cache stream')])
    now=datetime.datetime.now(datetime.timezone.utc)
    cert=(x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
          .serial_number(x509.random_serial_number()).not_valid_before(now-datetime.timedelta(minutes=1))
          .not_valid_after(now+datetime.timedelta(minutes=5)).add_extension(x509.SubjectAlternativeName([x509.IPAddress(ip)]),critical=False)
          .add_extension(x509.KeyUsage(digital_signature=True,content_commitment=False,key_encipherment=False,
                                      data_encipherment=False,key_agreement=False,key_cert_sign=True,crl_sign=True,
                                      encipher_only=False,decipher_only=False),critical=True)
          .add_extension(x509.BasicConstraints(ca=True,path_length=0),critical=True).sign(key,hashes.SHA256()))
    return (cert.public_bytes(serialization.Encoding.PEM).decode(),
            key.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,serialization.NoEncryption()).decode())


def copy(source, source_cache, destination, destination_cache, receive, *, timeout=90):
    from scripts.swarm_runtime import CONFIG
    settings=json.loads(CONFIG.read_text());address=settings['peers'][source['host']]
    for host,cache in ((source,source_cache),(destination,destination_cache)):
        if host['host'] not in settings['peers']:raise ValueError('Unenrolled cache transfer node')
        key,_=contract(cache['image'],cache['files'])
        if cache.get('key')!=key or cache.get('container')!='swarm-job-'+key[:32]:raise ValueError('Cache transfer binding differs')
    if source_cache['files']!=destination_cache['files']:raise ValueError('Cache transfer objects differ')
    cert,key=certificate(address);common={'token':secrets.token_hex(32),'address':address,'certificate':cert}
    sender={**common,'mode':'send','machine':source['machine'],'cache':source_cache,'private_key':key,'check':CHECK}
    # Always use the enrolled command API, including the controller's own node.
    def argv(host):return shell_command({**host,'local':False},['python3','-c',RUNTIME])
    started=time.monotonic();process=subprocess.Popen(argv(source),stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
    try:
        process.stdin.write(json.dumps(sender));process.stdin.close();process.stdin=None
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout,selectors.EVENT_READ)
            if not selector.select(min(timeout,30)):raise TimeoutError('Cache source did not become ready')
        announcement=json.loads(process.stdout.readline());port=announcement['port']
        if type(port) is not int or not 1024<=port<=65535:raise ValueError('Invalid cache transfer port')
        receiver={**common,'mode':'receive','machine':destination['machine'],'cache':destination_cache,'port':port,'receive':receive}
        result=subprocess.run(argv(destination),input=json.dumps(receiver),capture_output=True,text=True,timeout=max(1,timeout-(time.monotonic()-started)))
        if result.returncode:raise RuntimeError('Direct cache receiver unavailable or rejected contents')
        _,errors=process.communicate(timeout=max(1,timeout-(time.monotonic()-started)))
        if process.returncode:raise RuntimeError('Direct cache source failed')
        return {'source':source['host'],'destination':destination['host'],'transport':'direct-pinned-tls',
                'bytes':sum(v['bytes'] for v in source_cache['files'].values()),'seconds':time.monotonic()-started}
    finally:
        if process.poll() is None:
            process.terminate()
            try:process.wait(timeout=5)
            except subprocess.TimeoutExpired:process.kill();process.wait()
        for stream in (process.stdin,process.stdout,process.stderr):
            if stream is not None:stream.close()
