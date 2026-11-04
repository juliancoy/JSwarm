#!/usr/bin/env python3
"""Enroll independent bulk services using the standard identity-checked SSH deployer.

Never restarts launchers, conversations, or compute jobs. Offline peers retain
SSH fallback and can be enrolled by rerunning this idempotent deployment.
"""
import argparse
import base64
import datetime
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT=(Path(__file__).resolve().parents[2] if Path(__file__).resolve().parents[1].name in ('GenomeHarbor', 'JSwarm') else Path(__file__).resolve().parents[1])
sys.path.insert(0,str(ROOT))
sys.path.insert(0,str(ROOT/'swarm-control/server'))
from agent_client import AgentClient, CONFIG
from agent_updates import LauncherUpdates

INIT=r'''import hashlib,json,os,pathlib,subprocess,sys
p=json.load(sys.stdin);os.umask(0o077)
machine=hashlib.sha256(pathlib.Path('/etc/machine-id').read_bytes()).hexdigest()
enrolled=json.loads((pathlib.Path.home()/'.config/lifetech-launcher/peers.json').read_text())
if machine!=p['machine'] or enrolled['name']!=p['node'] or enrolled['controller_public_key']!=p['authority']:raise ValueError('Enrolled machine mismatch')
root=pathlib.Path.home()/'.local/state/lifetech-launcher/bulk-transport';root.mkdir(parents=True,exist_ok=True,mode=0o700)
if not (root/'identity.key').exists():
 subprocess.run(['openssl','req','-new','-newkey','ec','-pkeyopt','ec_paramgen_curve:P-256','-nodes','-keyout',str(root/'identity.key'),'-out',str(root/'identity.csr'),'-subj','/CN='+p['node']],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
print(json.dumps({'root':str(root),'csr':(root/'identity.csr').read_text(),'machine':machine}))
'''
INSTALL=r'''import base64,hashlib,json,os,pathlib,subprocess,sys
p=json.load(sys.stdin);os.umask(0o077)
if hashlib.sha256(pathlib.Path('/etc/machine-id').read_bytes()).hexdigest()!=p['config']['machine']:raise ValueError('Machine changed')
root=pathlib.Path(p['root']);(root/'objects').mkdir(exist_ok=True,mode=0o700)
revision=hashlib.sha256(json.dumps(p['files'],sort_keys=True).encode()).hexdigest()
release=root/'releases'/revision;release.mkdir(parents=True,exist_ok=True,mode=0o700)
for name,text in p['files'].items():
 if name not in ('transfer.py','bulk.py','stream_verify.py'):raise ValueError('Unexpected release member')
 compile(text,name,'exec');target=release/name
 if target.exists() and target.read_text()!=text:raise ValueError('Immutable release changed')
 target.write_text(text)
for name,text in [('ca.crt',p['ca']),('identity.crt',p['cert']),('config.json',json.dumps(p['config']))]:
 pending=root/(name+'.tmp');pending.write_text(text);pending.chmod(0o600);pending.replace(root/name)
descriptor=pathlib.Path.home()/'.config/lifetech-launcher/bulk-streams.json'
metadata={'root':str(root),'config_dir':str(descriptor.parent),'config':p['config']}
pending=descriptor.with_suffix('.tmp');pending.write_text(json.dumps(metadata));pending.chmod(0o600);pending.replace(descriptor)
pending=root/'current.tmp';pending.unlink(missing_ok=True);pending.symlink_to(release);pending.replace(root/'current')
unit=pathlib.Path.home()/'.config/systemd/user/lifetech-bulk-https.service';unit.parent.mkdir(parents=True,exist_ok=True)
old_unit=unit.read_text() if unit.exists() else None
unit.write_text('[Unit]\nDescription=Enrolled mutual TLS bulk data transport\nAfter=network-online.target\n[Service]\nExecStart=/usr/bin/python3 '+str(root/'current/transfer.py')+' serve --bind '+p['config']['address']+' --port '+str(p['config']['port'])+' --manifest '+str(root/'objects')+' --ca '+str(root/'ca.crt')+' --cert '+str(root/'identity.crt')+' --key '+str(root/'identity.key')+'\nUMask=0077\nNoNewPrivileges=true\nRestart=on-failure\nRestartSec=10\n[Install]\nWantedBy=default.target\n')
subprocess.run(['systemctl','--user','daemon-reload'],check=True)
if old_unit is not None and old_unit!=unit.read_text():subprocess.run(['systemctl','--user','restart',unit.name],check=True)
# Reload this data-only service at an idle point when code/certificates renew.
version=root/'server-version.json';wanted={'revision':revision,'certificate':hashlib.sha256(p['cert'].encode()).hexdigest()}
previous=json.loads(version.read_text()) if version.exists() else None
active=subprocess.run(['systemctl','--user','is-active','--quiet',unit.name]).returncode==0
if active and previous!=wanted:
 pid=subprocess.check_output(['systemctl','--user','show','--property=MainPID','--value',unit.name],text=True).strip()
 if pid.isdigit() and len(list((pathlib.Path('/proc')/pid/'task').iterdir()))==1:
  subprocess.run(['systemctl','--user','restart',unit.name],check=True);active=False
subprocess.run(['systemctl','--user','enable','--now',unit.name],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
if not active:version.write_text(json.dumps(wanted))
print(json.dumps({'node':p['config']['name'],'revision':revision,'active':subprocess.run(['systemctl','--user','is-active','--quiet',unit.name]).returncode==0,'active_jobs_restarted':False}))
'''


def deploy():
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID, ExtendedKeyUsageOID
    os.umask(0o077)
    settings=json.loads(CONFIG.read_text())
    authority=Path.home()/'.config/map-master/bulk-transport-ca';authority.mkdir(mode=0o700,exist_ok=True)
    now=datetime.datetime.now(datetime.timezone.utc)
    if not (authority/'ca.key').exists():
        key=ec.generate_private_key(ec.SECP256R1());subject=x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,'Enrolled swarm bulk transport')])
        cert=(x509.CertificateBuilder().subject_name(subject).issuer_name(subject).public_key(key.public_key()).serial_number(x509.random_serial_number()).not_valid_before(now-datetime.timedelta(minutes=5)).not_valid_after(now+datetime.timedelta(days=3650)).add_extension(x509.BasicConstraints(ca=True,path_length=0),critical=True).sign(key,hashes.SHA256()))
        (authority/'ca.key').write_bytes(key.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,serialization.NoEncryption()))
        (authority/'ca.crt').write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    ca_key=serialization.load_pem_private_key((authority/'ca.key').read_bytes(),password=None)
    ca=x509.load_pem_x509_certificate((authority/'ca.crt').read_bytes())
    # Reuse the standard deployer's pinned SSH construction and machine proof.
    updater=object.__new__(LauncherUpdates)
    def execute(host,code,payload):
        result=subprocess.run(updater.command(host,code),input=json.dumps(payload),capture_output=True,text=True,timeout=45)
        if result.returncode:raise RuntimeError('Identity-checked bulk deployment failed: '+result.stderr[-180:])
        return json.loads(result.stdout)
    nodes={};pending={}
    for name,address in settings['peers'].items():
        host={'host':name,'local':name==settings['name'],'lan_address':address,'agent_endpoint':'http://'+address+':8790'}
        client=AgentClient(host)
        try:
            status=client.call('status',timeout=5)
            if status['node']!=name or address not in status.get('addresses',[]):raise ValueError('Agent address/name mismatch')
            data=execute(host,INIT,{'machine':status['machine'],'node':name,'authority':settings['controller_public_key']})
            csr=x509.load_pem_x509_csr(data['csr'].encode())
            if not csr.is_signature_valid:raise ValueError('Invalid CSR')
            cert=(x509.CertificateBuilder().subject_name(csr.subject).issuer_name(ca.subject).public_key(csr.public_key()).serial_number(x509.random_serial_number()).not_valid_before(now-datetime.timedelta(minutes=5)).not_valid_after(now+datetime.timedelta(days=90)).add_extension(x509.BasicConstraints(ca=False,path_length=None),critical=True).add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address(address))]),critical=False).add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH,ExtendedKeyUsageOID.CLIENT_AUTH]),critical=False).sign(ca_key,hashes.SHA256()))
            saved=authority/(name+'.crt')
            if saved.exists():
                previous=x509.load_pem_x509_certificate(saved.read_bytes())
                old_key=previous.public_key().public_bytes(serialization.Encoding.DER,serialization.PublicFormat.SubjectPublicKeyInfo)
                new_key=cert.public_key().public_bytes(serialization.Encoding.DER,serialization.PublicFormat.SubjectPublicKeyInfo)
                old_expiry=previous.not_valid_after.replace(tzinfo=datetime.timezone.utc)
                old_addresses=previous.extensions.get_extension_for_class(x509.SubjectAlternativeName).value.get_values_for_type(x509.IPAddress)
                if old_key==new_key and ipaddress.ip_address(address) in old_addresses and old_expiry>now+datetime.timedelta(days=30):cert=previous
            saved.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
            public=cert.public_key().public_bytes(serialization.Encoding.DER,serialization.PublicFormat.SubjectPublicKeyInfo)
            nodes[name]={'host':host,'cert':cert.public_bytes(serialization.Encoding.PEM).decode(),'root':data['root'],'machine':data['machine'],'address':address,'certificate_sha256':cert.fingerprint(hashes.SHA256()).hex(),'public_key_pin':'sha256//'+base64.b64encode(hashlib.sha256(public).digest()).decode()}
        except Exception as error:pending[name]=str(error)[:180]
        finally:client.close()
    registry=authority/'enrolled-peers.json'
    retained=json.loads(registry.read_text()) if registry.exists() else {}
    peers={name:value for name,value in retained.items() if name in settings['peers'] and value['address']==settings['peers'][name]}
    peers.update({name:{k:value[k] for k in ['root','machine','address','certificate_sha256','public_key_pin']} for name,value in nodes.items()})
    pending_registry=registry.with_suffix('.tmp');pending_registry.write_text(json.dumps(peers));pending_registry.replace(registry)
    files={'transfer.py':(ROOT/'scripts/swarm_tls_transfer.py').read_text(),'bulk.py':(ROOT/'scripts/swarm_bulk.py').read_text(),'stream_verify.py':(ROOT/'scripts/swarm_stream_verify.py').read_text()}
    installed=[]
    for name,node in nodes.items():
        config={'name':name,'machine':node['machine'],'address':node['address'],'port':9448,'min_bytes':64*1024**2,'peers':peers}
        try:
            result=execute(node['host'],INSTALL,{'root':node['root'],'files':files,'config':config,'ca':(authority/'ca.crt').read_text(),'cert':node['cert']})
            installed.append(result);print(json.dumps(result),flush=True)
        except Exception as error:pending[name]=str(error)[:180]
    report={'installed':installed,'pending':pending}
    target=Path.home()/'.local/state/swarm-control/bulk-deployment.json';target.parent.mkdir(mode=0o700,parents=True,exist_ok=True);target.write_text(json.dumps(report,indent=2));target.chmod(0o600)
    for name,reason in pending.items():print(json.dumps({'node':name,'pending':reason}),flush=True)
    return report


if __name__=='__main__':
    import fcntl
    os.umask(0o077)
    lock_path=Path.home()/'.config/map-master/.bulk-deployment.lock'
    with lock_path.open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        deploy()
