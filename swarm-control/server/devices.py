"""Owner-issued, one-use phone pairing and revocable device sessions."""
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import secrets
import time

FILE=Path(os.getenv('MAP_CODEX_DEVICE_FILE',str(Path.home()/'.config/swarm-control/devices.json')))
LIFETIME=30*86400

def digest(token):return hashlib.sha256(token.encode()).hexdigest()

@contextmanager
def state():
    FILE.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    fd=os.open(FILE.with_suffix('.lock'),os.O_CREAT|os.O_RDWR,0o600)
    with os.fdopen(fd,'r+') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        value=json.loads(FILE.read_text()) if FILE.exists() else {'pairs':{},'sessions':{}}
        now=time.time()
        value['pairs']={k:v for k,v in value['pairs'].items() if v>now}
        value['sessions']={k:v for k,v in value['sessions'].items() if v['expires']>now}
        yield value

def save(value):
    p=FILE.with_suffix('.tmp')
    fd=os.open(p,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
    with os.fdopen(fd,'w') as stream:json.dump(value,stream)
    p.chmod(0o600);p.replace(FILE)

def issue():
    token=secrets.token_urlsafe(32)
    with state() as value:
        # Only the newest setup link remains valid.
        value['pairs']={digest(token):time.time()+600};save(value)
    return token

def new_session(value,label='Browser',pidp_identity=None,pidp_subject=None):
    token=secrets.token_urlsafe(32)
    now=time.time()
    value['sessions'][digest(token)]={'label':label,'created':now,'last_seen':now,'expires':now+LIFETIME,**({'auth_method':'pidp','pidp_identity':pidp_identity,'pidp_subject':pidp_subject} if pidp_identity else {})};save(value)
    return token

def session(pair):
    if not isinstance(pair,str) or not 20<=len(pair)<=128:raise ValueError('Invalid pairing token')
    with state() as value:
        if digest(pair) not in value['pairs']:raise ValueError('Pairing link expired or already used. Request a fresh link from CompAI.')
        del value['pairs'][digest(pair)]
        return new_session(value)

def valid(token):
    if not isinstance(token,str) or not 20<=len(token)<=128:return False
    with state() as value:
        record=value['sessions'].get(digest(token))
        if record and time.time()-record['last_seen']>=60:
            record['last_seen']=time.time();save(value)
        return record is not None

def describe(token):
    if not valid(token):return None
    with state() as value:
        return value['sessions'].get(digest(token))

def revoke(token=None):
    with state() as value:
        if token is None:value={'pairs':{},'sessions':{}}
        else:value['sessions'].pop(digest(token),None)
        save(value)


def pidp_valid(token):
    if not valid(token):return False
    with state() as value:
        record=value['sessions'].get(digest(token),{})
        return record.get('auth_method')=='pidp' and isinstance(record.get('pidp_identity'),str) and bool(record['pidp_identity'])
