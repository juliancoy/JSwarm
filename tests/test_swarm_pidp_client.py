import base64
from concurrent.futures import ThreadPoolExecutor
import hashlib
import http.client
import json
import socket
import threading
import time
from urllib.parse import parse_qs,urlsplit,urlencode

import pytest

from scripts.swarm_pidp_client import PIdPClient


def configuration():
    with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
    return {'issuer':'https://identity.example','resource':'https://source.example/genomics',
            'client_id':'native-genomics','redirect_uri':f'http://127.0.0.1:{port}/callback'}


def test_pkce_callback_rejects_wrong_state_and_checks_issuer(monkeypatch,capsys):
    client=PIdPClient(configuration());observed={};threads=[]
    def open_browser(url):
        observed['url']=url;query=parse_qs(urlsplit(url).query);redirect=urlsplit(client.config['redirect_uri'])
        def callback():
            for state in ('wrong',query['state'][0]):
                connection=http.client.HTTPConnection('127.0.0.1',redirect.port,timeout=5)
                connection.request('GET',redirect.path+'?'+urlencode({'state':state,'iss':client.config['issuer'],'code':'authorization-code'}))
                response=connection.getresponse();response.read();connection.close()
                observed.setdefault('statuses',[]).append(response.status)
        thread=threading.Thread(target=callback);thread.start();threads.append(thread)
    def exchange(fields):observed['fields']=fields;client.tokens={'access_token':'access','refresh_token':'refresh','deadline':time.monotonic()+300}
    monkeypatch.setattr('scripts.swarm_pidp_client.webbrowser.open',open_browser)
    monkeypatch.setattr(client,'exchange',exchange)
    assert client.login(timeout=10) is client
    for thread in threads:thread.join()
    assert observed['statuses']==[400,200]
    query=parse_qs(urlsplit(observed['url']).query)
    challenge=base64.urlsafe_b64encode(hashlib.sha256(observed['fields']['code_verifier'].encode()).digest()).rstrip(b'=').decode()
    assert query['code_challenge']==[challenge] and query['scope']==['genomics:data']
    assert 'authorization-code' not in capsys.readouterr().out


def test_rotating_refresh_is_serialized_and_not_retried(monkeypatch):
    client=PIdPClient(configuration());calls=[]
    client.tokens={'access_token':'old','refresh_token':'rotating','deadline':0}
    def exchange(fields):
        calls.append(fields);time.sleep(.01)
        client.tokens={'access_token':'new','refresh_token':'next','deadline':time.monotonic()+300}
    monkeypatch.setattr(client,'exchange',exchange)
    with ThreadPoolExecutor(max_workers=4) as pool:assert list(pool.map(lambda _:client(),range(4)))==['new']*4
    assert len(calls)==1 and calls[0]['refresh_token']=='rotating'
    client.tokens['deadline']=0
    def unavailable(fields):raise OSError('network unavailable')
    monkeypatch.setattr(client,'exchange',unavailable)
    with pytest.raises(OSError):client()
    with pytest.raises(ValueError,match='login required'):client()


@pytest.mark.parametrize('field,value',[('issuer','http://identity.example'),('redirect_uri','https://evil.example/callback'),
                                       ('redirect_uri','http://localhost:8917/callback')])
def test_native_client_rejects_untrusted_endpoints(field,value):
    config=configuration();config[field]=value
    with pytest.raises(ValueError):PIdPClient(config)
