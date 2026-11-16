"""Native cache, actual FUSE mmap, binary peers, mTLS and dual-stack recovery."""
from concurrent.futures import ThreadPoolExecutor
import datetime
import hashlib
import http.client
import json
import mmap
import os
from pathlib import Path
import socket
import ssl
import subprocess
import threading
import time

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes,serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID,ExtendedKeyUsageOID
from scripts.swarm_memory import seal
from scripts.swarm_tls_transfer import make_server,identity

BINARY=Path(os.environ.get('SWARM_MEMORY_BINARY','/tmp/swarm-native-build/swarm-memory'))


def port():
    with socket.socket(socket.AF_INET6,socket.SOCK_STREAM) as s:s.bind(('::1',0));return s.getsockname()[1]


@pytest.fixture
def environment(tmp_path):
    now=datetime.datetime.now(datetime.timezone.utc);ca_key=ec.generate_private_key(ec.SECP256R1())
    issuer=x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,'native-fixture-ca')])
    ca=(x509.CertificateBuilder().subject_name(issuer).issuer_name(issuer).public_key(ca_key.public_key()).serial_number(1)
        .not_valid_before(now-datetime.timedelta(minutes=1)).not_valid_after(now+datetime.timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True,path_length=None),critical=True).sign(ca_key,hashes.SHA256()))
    peers={};roots={};ports={'a.local':port(),'b.local':port()}
    machine=hashlib.sha256(Path('/etc/machine-id').read_bytes()).hexdigest()
    for serial,name in enumerate(['origin.local','a.local','b.local','outsider.local'],2):
        root=tmp_path/name;root.mkdir();roots[name]=root
        key=ec.generate_private_key(ec.SECP256R1());subject=x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,name)])
        cert=(x509.CertificateBuilder().subject_name(subject).issuer_name(issuer).public_key(key.public_key()).serial_number(serial)
              .not_valid_before(now-datetime.timedelta(minutes=1)).not_valid_after(now+datetime.timedelta(days=1))
              .add_extension(x509.SubjectAlternativeName([x509.DNSName(name)]),critical=False)
              .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CLIENT_AUTH,ExtendedKeyUsageOID.SERVER_AUTH]),critical=False)
              .sign(ca_key,hashes.SHA256()))
        (root/'ca.crt').write_bytes(ca.public_bytes(serialization.Encoding.PEM))
        (root/'identity.crt').write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        (root/'identity.key').write_bytes(key.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,serialization.NoEncryption()))
        peers[name]={'address':name,'machine':machine,'certificate_sha256':hashlib.sha256(cert.public_bytes(serialization.Encoding.DER)).hexdigest()}
    data=bytes(range(251))*40000;source=tmp_path/'original';source.write_bytes(data);source.chmod(0o400)
    manifest=seal(source,hashlib.sha256(data).hexdigest())
    objects=tmp_path/'grants';objects.mkdir();grant='c'*64
    row={'path':str(source),'sha256':manifest['sha256'],'identity':identity(source.stat()),'clients':[peers[n]['certificate_sha256'] for n in ['a.local','b.local']]}
    (objects/(grant+'.json')).write_text(json.dumps(row))
    origin=roots['origin.local'];server=make_server('::',0,objects,origin/'identity.crt',origin/'identity.key',origin/'ca.crt')
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    peers.pop('outsider.local')
    definition={'source':'origin.local','machine':machine,'sha256':manifest['sha256'],'bytes':len(data),'clients':{'a.local':grant,'b.local':grant}}
    discovery=tmp_path/'discovery.json'
    hosts={n:[{'family':6,'address':'::1','scope':0},{'family':4,'address':'127.0.0.1','scope':0}] for n in peers}
    def snapshot():discovery.write_text(json.dumps({'updated':int(time.time()),'hosts':hosts}))
    snapshot();configs={}
    for name in ['a.local','b.local']:
        root=roots[name];(root/'config.json').write_text(json.dumps({'name':name,'machine':machine,'peers':peers,'port':server.server_port}))
        mount=tmp_path/(name+'-mount');mount.mkdir()
        config={'tls_root':str(root),'discovery':str(discovery),'cache_bytes':512*1024,'peer_port':ports[name],'peer_ports':ports,'replicas':0,
                'objects':[{'manifest':manifest,'sources':[definition] if name=='a.local' else [],'mountpoint':str(mount),
                            'peers':[] if name=='a.local' else ['a.local'],'allowed':['a.local','b.local']}]}
        path=tmp_path/(name+'.json');path.write_text(json.dumps(config));configs[name]=(path,config)
    value=dict(roots=roots,ports=ports,data=data,manifest=manifest,configs=configs,source=source,grants=objects,row=row,hosts=hosts,snapshot=snapshot)
    try:yield value
    finally:server.shutdown();server.server_close();thread.join(timeout=3)


def run_read(env,name='a.local',offset=0,size=65536):
    env['snapshot']();path,_=env['configs'][name]
    return subprocess.run([str(BINARY),'--config',str(path),'--read',env['manifest']['id'],str(offset),str(size)],capture_output=True,timeout=25)


def test_cache_concurrency_and_shared_budget():
    subprocess.run([str(BINARY.with_name('swarm-memory-cache-test'))],check=True,timeout=30)


def test_native_source_ranges_integrity_pins_and_address_family_fallback(environment):
    env=environment
    # An unavailable IPv6 route cannot hide a live IPv4 path.
    env['hosts']['origin.local']=[{'family':6,'address':'::2','scope':0},{'family':4,'address':'127.0.0.1','scope':0}]
    result=run_read(env,offset=17,size=190000)
    assert result.returncode==0,result.stderr.decode()
    assert result.stdout==env['data'][17:190017]
    stats=json.loads(result.stderr);assert stats['transport']['ipv4_connections']==1
    assert stats['counters']['source_requests']==1 and stats['counters']['source_reads']==4
    # Fresh discovery after IPv4 disappears preserves the working IPv6 route.
    env['hosts']['origin.local']=[{'family':6,'address':'::1','scope':0}]
    result=run_read(env,offset=len(env['data'])-100,size=4096)
    assert result.returncode==0,result.stderr.decode()
    assert result.stdout==env['data'][-100:]
    assert json.loads(result.stderr)['transport']['ipv6_connections']==1
    root=env['roots']['a.local'];config=json.loads((root/'config.json').read_text())
    config['peers']['origin.local']['certificate_sha256']='0'*64;(root/'config.json').write_text(json.dumps(config))
    result=run_read(env);assert result.returncode!=0 and b'certificate pin differs' in result.stderr


def test_native_rejects_corrupt_manifest_and_source(environment):
    env=environment;path,config=env['configs']['a.local']
    config['objects'][0]['manifest']['pages'][0]='0'*64;path.write_text(json.dumps(config))
    result=run_read(env);assert result.returncode!=0 and b'manifest identity' in result.stderr
    config['objects'][0]['manifest']['pages'][0]=hashlib.sha256(env['data'][:65536]).hexdigest();path.write_text(json.dumps(config))
    source=env['source'];source.chmod(0o600);source.write_bytes(b'x'*len(env['data']));source.chmod(0o400)
    env['row']['identity']=identity(source.stat());(env['grants']/('c'*64+'.json')).write_text(json.dumps(env['row']))
    result=run_read(env);assert result.returncode!=0 and b'page hash differs' in result.stderr


def peer_request(env,name,method,path,data=b'',client='b.local'):
    root=env['roots'][client];context=ssl.create_default_context(cafile=str(root/'ca.crt'))
    context.load_cert_chain(root/'identity.crt',root/'identity.key')
    connection=http.client.HTTPSConnection(name,env['ports'][name],context=context,timeout=4)
    connection._create_connection=lambda *args,**kwargs:socket.create_connection(('::1',env['ports'][name]),timeout=4)
    try:
        connection.request(method,path,body=data);r=connection.getresponse();return r.status,r.read()
    finally:connection.close()


def test_actual_fuse_mmap_peer_only_reads_and_binary_admission(environment,tmp_path):
    if not Path('/dev/fuse').exists():pytest.skip('Host FUSE is unavailable')
    env=environment;processes=[];logs=[]
    try:
        for name in ['a.local','b.local']:
            path,config=env['configs'][name];log=(tmp_path/(name+'.log')).open('w+');logs.append(log)
            p=subprocess.Popen([str(BINARY),'--config',str(path)],stdout=log,stderr=log);processes.append(p)
            mount=Path(config['objects'][0]['mountpoint']);deadline=time.monotonic()+10
            while not os.path.ismount(mount) and p.poll() is None and time.monotonic()<deadline:time.sleep(.05)
            log.seek(0);assert os.path.ismount(mount),log.read()
        a=Path(env['configs']['a.local'][1]['objects'][0]['mountpoint']);b=Path(env['configs']['b.local'][1]['objects'][0]['mountpoint'])
        with (a/'object').open('rb') as f,mmap.mmap(f.fileno(),0,access=mmap.ACCESS_READ) as m:
            assert m[3:30000]==env['data'][3:30000]
        # B has no authoritative sources at all. Its read must come from A's
        # real native binary cache, using inventory hints and enrolled mTLS.
        with (b/'object').open('rb',buffering=0) as f:
            assert os.pread(f.fileno(),10000,7)==env['data'][7:10007]
        stats=json.loads((b/'metrics.json').read_text());assert stats['counters']['peer_hits']>0
        assert stats['counters'].get('source_requests',0)==0
        base='/v1/cache/'+env['manifest']['id']+'/'
        assert peer_request(env,'a.local','PUT',base+'0',b'x'*65536)[0]==422
        for page in range(8,24):
            start=page*65536;assert peer_request(env,'a.local','PUT',base+str(page),env['data'][start:start+65536])[0]==201
        code,bits=peer_request(env,'a.local','GET',base+'inventory')
        assert code==200 and not bits[0]&1
        assert json.loads((a/'metrics.json').read_text())['cache_used_bytes']<=512*1024
        with pytest.raises((OSError,http.client.HTTPException)):
            peer_request(env,'a.local','GET',base+'inventory',client='outsider.local')
        with pytest.raises(OSError):(a/'object').open('r+b')
    finally:
        for p in processes:
            if p.poll() is None:p.terminate()
        for p in processes:
            try:p.wait(timeout=15)
            except subprocess.TimeoutExpired:p.kill();p.wait();raise
        for log in logs:log.close()
