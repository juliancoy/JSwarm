"""PIdP-approved native node enrollment with an S256 PKCE delivery grant.

The browser sees machine identity only. Fleet provisioning is delivered solely
on the native HTTPS exchange, never in a redirect, URL, cookie or browser token.
"""
import base64
from contextlib import contextmanager
import fcntl
import hashlib
import hmac
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import time
import uuid
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
import devices
import identity
from agent_client import CONFIG

STORE=Path.home()/'.config/swarm-control/node-enrollment.sqlite'
SIGNING_KEY=Path.home()/'.config/organization-peers/signing.key'
LIFETIME=600


def b64(raw):return base64.urlsafe_b64encode(raw).rstrip(b'=').decode()


@contextmanager
def database():
    STORE.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
    descriptor=os.open(STORE,os.O_CREAT|os.O_RDWR,0o600);os.close(descriptor)
    STORE.chmod(0o600)
    with sqlite3.connect(STORE,timeout=5) as db:
        db.row_factory=sqlite3.Row
        db.execute('CREATE TABLE IF NOT EXISTS grants(id TEXT PRIMARY KEY, request TEXT NOT NULL, expires REAL NOT NULL, approver TEXT, delivery BLOB)')
        db.execute('BEGIN IMMEDIATE')
        db.execute('DELETE FROM grants WHERE expires<=?',(time.time(),))
        yield db


def start(origin,body):
    if not isinstance(body,dict) or set(body)!={'name','machine','address','code_challenge','code_challenge_method'}:raise ValueError('Expected node identity and S256 challenge')
    if body['code_challenge_method']!='S256' or not isinstance(body['code_challenge'],str) or not re.fullmatch('[A-Za-z0-9_-]{43}',body['code_challenge']):raise ValueError('S256 is required')
    if not isinstance(body['name'],str) or not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,62}\.local',body['name']):raise ValueError('Invalid node name')
    if not isinstance(body['machine'],str) or not re.fullmatch('[a-f0-9]{64}',body['machine']):raise ValueError('Invalid machine identity')
    address=ipaddress.IPv4Address(body['address'])
    if address.is_loopback or address.is_unspecified or address.is_multicast or address.is_link_local:raise ValueError('Use a physical interface address')
    config=json.loads(CONFIG.read_text())
    if not config.get('internet') or not config.get('internet_nodes',{}).get(config.get('master')) or not SIGNING_KEY.is_file():raise RuntimeError('WAN enrollment is not configured on the coordinator')
    ident=secrets.token_urlsafe(32)
    with database() as db:
        if db.execute('SELECT COUNT(*) FROM grants').fetchone()[0]>=128:raise RuntimeError('Too many pending node sign-ins')
        db.execute('INSERT INTO grants VALUES(?,?,?,NULL,NULL)',(ident,json.dumps(body),time.time()+LIFETIME))
    return {'request_id':ident,'authorization_url':origin+'/join-node?request='+ident,'expires_in':LIFETIME,'interval':3}


def details(ident):
    with database() as db:
        row=db.execute('SELECT * FROM grants WHERE id=?',(ident,)).fetchone()
        if row is None:raise ValueError('Node sign-in expired')
        value=json.loads(row['request'])
        return {k:value[k] for k in ('name','machine','address')}|{'request_id':ident,'expires':row['expires'],'approved':bool(row['approver'])}


def approve(ident,token,origin):
    if not identity.valid_session(origin,token):raise PermissionError('PIdP owner sign-in required')
    with database() as db:
        row=db.execute('SELECT * FROM grants WHERE id=?',(ident,)).fetchone()
        if row is None or row['delivery'] is not None:raise ValueError('Node sign-in expired or already delivered')
        db.execute('UPDATE grants SET approver=? WHERE id=?',(devices.digest(token),ident))
    return {'approved':True}


def provision(request,ident,key):
    # Serialize fleet mutations across HTTP workers and enrollment retries.
    with CONFIG.with_suffix('.enrollment.lock').open('a+') as lock:
        os.chmod(lock.name,0o600);fcntl.flock(lock,fcntl.LOCK_EX)
        config=json.loads(CONFIG.read_text());name=request['name'];machine=request['machine']
        config['enrollment_deliveries']={k:v for k,v in config.get('enrollment_deliveries',{}).items() if v['expires']>time.time()}
        saved=config['enrollment_deliveries'].get(ident)
        if saved:
            raw=base64.urlsafe_b64decode(saved['sealed'])
            return json.loads(AESGCM(key).decrypt(raw[:12],raw[12:],ident.encode()))
        if name in config['peers'] or name in config.get('retired_peers',[]):raise ValueError('Node name is already enrolled or retired; choose a new name')
        if len(config['peers'])>=24:raise ValueError('Fleet is full')
        if machine in config.get('enrolled_machines',{}).values():raise ValueError('Machine is already enrolled')
        if SIGNING_KEY.stat().st_mode&0o077:raise PermissionError('Private enrollment signing key required')
        secret=SIGNING_KEY.read_text().strip()
        if len(secret)<32:raise ValueError('Invalid enrollment signing key')
        internet=config['internet'];now=int(time.time());peer=str(uuid.uuid4())
        claims={'aud':'launcher-enroll-v1','room':internet['room'],'peer':peer,'iat':now,'exp':now+LIFETIME}
        signed=b64(b'{"alg":"HS256","typ":"JWT"}')+'.'+b64(json.dumps(claims,separators=(',',':')).encode())
        ticket=signed+'.'+b64(hmac.new(secret.encode(),signed.encode(),hashlib.sha256).digest())
        config['peers'][name]=request['address'];config.setdefault('internet_nodes',{})[name]=peer
        config.setdefault('enrolled_machines',{})[name]=machine
        config['wan_only']=list(dict.fromkeys(config.get('wan_only',[])+[name]))
        master=config.get('master',config['name'])
        value={'name':name,'master':master,'key':config['key'],'controller_public_key':config['controller_public_key'],
               'peers':{master:config['peers'][master],name:request['address']},
               'internet_nodes':{master:config['internet_nodes'][master],name:peer},'wan_only':[master],
               'stream_state_directory':'stream-state'}
        delivery={'config':value,'invitation':{'url':internet['url'],'token':ticket}}
        nonce=secrets.token_bytes(12)
        sealed=nonce+AESGCM(key).encrypt(nonce,json.dumps(delivery).encode(),ident.encode())
        config['enrollment_deliveries'][ident]={'expires':now+LIFETIME,'sealed':base64.urlsafe_b64encode(sealed).decode()}
        import tempfile
        with tempfile.NamedTemporaryFile(mode='w',dir=CONFIG.parent,delete=False) as handle:
            json.dump(config,handle);handle.flush();os.fsync(handle.fileno());temporary=Path(handle.name)
        temporary.chmod(0o600);temporary.replace(CONFIG)
        return delivery


def exchange(origin,body):
    if not isinstance(body,dict) or set(body)!={'request_id','code_verifier'}:raise ValueError('Expected native PKCE exchange')
    verifier=body['code_verifier']
    if not isinstance(verifier,str) or not re.fullmatch('[A-Za-z0-9._~-]{43,128}',verifier):raise ValueError('Invalid PKCE verifier')
    challenge=hashlib.sha256(verifier.encode()).digest()
    key=hashlib.sha256(b'jswarm-node-delivery.v1\0'+verifier.encode()).digest()
    with database() as db:
        row=db.execute('SELECT * FROM grants WHERE id=?',(body['request_id'],)).fetchone()
        if row is None:raise ValueError('Node sign-in expired')
        request=json.loads(row['request'])
        if not hmac.compare_digest(b64(challenge),request['code_challenge']):raise PermissionError('PKCE verification failed')
        if not row['approver']:return {'status':'authorization_pending'}
        cfg=identity.config(origin)
        with devices.state() as state:owner=state['sessions'].get(row['approver'],{})
        if (owner.get('auth_method')!='pidp' or owner.get('pidp_identity')!=cfg['owner_canonical_user_id'] or
                owner.get('pidp_subject') not in cfg.get('allowed_subjects',[cfg['owner_subject']])):raise PermissionError('Approval session expired or revoked')
        aad=body['request_id'].encode()
        if row['delivery'] is not None:
            raw=bytes(row['delivery']);return json.loads(AESGCM(key).decrypt(raw[:12],raw[12:],aad))
        delivery={'status':'approved',**provision(request,body['request_id'],key)}
        nonce=secrets.token_bytes(12);sealed=nonce+AESGCM(key).encrypt(nonce,json.dumps(delivery).encode(),aad)
        db.execute('UPDATE grants SET delivery=? WHERE id=?',(sealed,body['request_id']))
        return delivery
