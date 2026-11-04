#!/usr/bin/env python3
"""Local-only registration backup authorization and non-mutating export checks."""
import hashlib
import json
import os
from pathlib import Path
import secrets
import sys
import urllib.error
import urllib.parse
import urllib.request

ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT/'scripts'))
from peer_archive import ticket

base=os.environ['PEER_TEST_URL'].rstrip('/')
if urllib.parse.urlsplit(base).hostname not in ('localhost','127.0.0.1'):raise RuntimeError('Synthetic registration checks require the local Worker')
key=Path(os.environ['PEER_TEST_KEY_FILE']).read_text().strip()
room=secrets.token_hex(32);credential=secrets.token_hex(32)

def call(path,token,body):
    request=urllib.request.Request(base+path,data=json.dumps(body).encode(),headers={'Authorization':'Bearer '+token,'Content-Type':'application/json'})
    try:
        with urllib.request.urlopen(request,timeout=15) as response:return response.status,json.load(response)
    except urllib.error.HTTPError as error:return error.code,json.load(error)

claims,invite=ticket(key,room,60,'launcher-enroll-v1')
assert call('/launchers/register',invite,{'name':'Synthetic recovery device','credential':credential})[0]==200
_,backup=ticket(key,room,60,'launcher-backup-v1')
status,first=call('/launchers/backup',backup,{})
assert status==200 and first['version']==1 and first['room']==room
assert len(first['devices'])==1 and first['devices'][0]['peer']==claims['peer']
assert first['devices'][0]['hash']==hashlib.sha256(credential.encode()).hexdigest()
assert credential not in json.dumps(first)
assert call('/launchers/backup',backup,{})[1]==first
assert call('/launchers/backup',invite,{})[0]==401
assert call('/launchers/backup',backup,{'objectId':first['objectId']})[0]==403
_,object_token=ticket(key,first['objectId'],60,'launcher-backup-v1')
assert call('/launchers/backup',object_token,{'objectId':first['objectId']})==(200,first)
_,admin=ticket(key,room,60,'launcher-admin-v1')
assert call('/launchers/revoke',admin,{'peer':claims['peer']})[0]==200
assert call('/launchers/backup',backup,{})[1]['devices'][0]['revoked'] is True
print('PASS: scoped read-only registration export, credential hashing, stable state and revocation recovery')
