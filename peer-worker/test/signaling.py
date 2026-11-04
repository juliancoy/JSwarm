#!/usr/bin/env python3
"""Exercise the real Worker/DO boundary, room isolation and ticket expiration."""
import hashlib
import uuid
import json
import os
from pathlib import Path
import sys
import time
import urllib.error
import urllib.request

from websockets.sync.client import connect
from websockets.exceptions import ConnectionClosed,InvalidStatus

ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT/'scripts'))
import peer_archive

BASE=os.environ.get('PEER_TEST_URL','http://localhost:8789')
KEY=Path(os.environ.get('PEER_TEST_KEY_FILE',ROOT/'peer-worker/.dev.vars'))
secret=KEY.read_text().strip()
if KEY.name=='.dev.vars':secret=next(x.split('=',1)[1] for x in secret.splitlines() if x.startswith('PEER_SIGNING_KEY='))
URL=BASE.replace('https:','wss:').replace('http:','ws:')+'/signal'

def ws(token,origin=BASE):return connect(URL,subprotocols=['archive.v1','auth.'+token],origin=origin,open_timeout=15)
def denied(token,code,origin=BASE):
    try:
        with ws(token,origin):raise AssertionError('Unauthorized connection accepted')
    except InvalidStatus as e:assert e.response.status_code==code,e.response.status_code

denied('invalid',401)
_,forged=peer_archive.ticket('wrong-signing-key-at-least-thirty-two-bytes','a'*64)
denied(forged,401)
a,ta=peer_archive.ticket(secret,'a'*64);b,tb=peer_archive.ticket(secret,'a'*64);c,tc=peer_archive.ticket(secret,'b'*64)
denied(ta,403,'https://unapproved.example')
with ws(ta) as first,ws(tb) as second,ws(tc) as other:
    assert json.loads(first.recv())['type']=='welcome'
    assert json.loads(first.recv())['type']=='joined'
    assert json.loads(second.recv())['peers']==[a['peer']]
    assert json.loads(other.recv())['peers']==[]
    denied(ta,409)
    description={'type':'offer','sdp':'synthetic test signal'}
    first.send(json.dumps({'type':'offer','to':c['peer'],'from':'spoofed','description':description}))
    try:other.recv(timeout=.3);raise AssertionError('Signal crossed dataset room')
    except TimeoutError:pass
    first.send(json.dumps({'type':'offer','to':b['peer'],'from':'spoofed','description':description}))
    forwarded=json.loads(second.recv(timeout=5));assert forwarded['from']==a['peer'] and forwarded['description']==description
    first.send(b'not a signaling message')
    try:first.recv(timeout=5);raise AssertionError('Binary signal accepted')
    except ConnectionClosed as e:assert e.rcvd.code==1009,e.rcvd
print('PASS authentication, origins, duplicate devices, room isolation, sender binding and message limits',flush=True)
_,short=peer_archive.ticket(secret,hashlib.sha256(uuid.uuid4().bytes).hexdigest(),3)
with ws(short) as expiring:
    expiring.recv(timeout=5)
    try:expiring.recv(timeout=45);raise AssertionError('Expired device stayed connected')
    except ConnectionClosed as e:assert e.rcvd.code==1008,e.rcvd
denied(short,401)
print('PASS server closes expired devices without waiting for a client message',flush=True)
