from contextlib import contextmanager
import http.client
import json
import threading

import pytest
from scripts import node_web as web


def config():
    return web.configuration({'name':'codecollective.local','peers':{'codecollective.local':'10.42.0.49','compai.local':'10.42.0.1','julian-minipc.local':'10.42.0.221'}})


@contextmanager
def running(config,view):
    server=web.ThreadingHTTPServer(('127.0.0.1',0),web.handler(config,view))
    worker=threading.Thread(target=server.serve_forever,daemon=True);worker.start()
    try:yield server.server_port
    finally:server.shutdown();server.server_close();worker.join()


def read(port,path='/',host='jswarm.localhost',method='GET',headers=None,body=None):
    c=http.client.HTTPConnection('127.0.0.1',port,timeout=3)
    c.request(method,path,body=body,headers={'Host':host,**(headers or {})})
    r=c.getresponse();result=(r.status,dict(r.getheaders()),r.read());c.close();return result


def test_aliases_reuse_enrollment_and_never_include_credentials():
    c=config()
    assert web.target(c,'jswarm.localhost')=='codecollective.local'
    assert web.target(c,'compai.jswarm.localhost')=='compai.local'
    assert web.target(c,'minipc.jswarm.localhost')=='julian-minipc.local'
    assert web.target(c,'compai.local:8797')=='compai.local'
    for host in ['attacker.test','compai.jswarm.localhost.evil','user@compai.jswarm.localhost','compai.jswarm.localhost:22','compai.jswarm.localhost/path']:
        with pytest.raises(ValueError):web.target(c,host)
    settings={'name':'compai.local','key':'secret','peers':{'compai.local':'10.42.0.1'}}
    assert 'secret' not in json.dumps(web.configuration(settings))


def test_local_pipeline_assets_and_machine_links(tmp_path):
    (tmp_path/'pipeline.html').write_text('local packaged pipeline')
    with running(config(),tmp_path) as port:
        assert read(port)[0]==302
        assert read(port)[1]['Location']=='/pipeline.html'
        status,headers,body=read(port,'/pipeline.html')
        assert status==200 and body==b'local packaged pipeline'
        assert headers['X-JSwarm-Node']=='codecollective.local'
        status,_,body=read(port,'/api/jswarm-web')
        context=json.loads(body)
        assert context['node']=='codecollective.local'
        assert context['nodes'][1]['url']=='http://compai.jswarm.localhost/pipeline.html'
        assert context['nodes'][1]['direct_url']=='http://10.42.0.1:8797/pipeline.html'
        assert read(port,'/../config.json')[0]==404
        assert read(port,'/%2e%2e/config.json')[0]==404
        assert read(port,host='evil.test')[0]==421
        assert read(port,'/api/tasks/machine/control',method='POST',headers={'Origin':'http://evil.test'})[0]==403
        assert read(port,'/api/tasks/machine/control',method='POST')[0]==403


def test_machine_alias_fetches_assets_from_that_machine(tmp_path,monkeypatch):
    (tmp_path/'pipeline.html').write_text('peer packaged pipeline')
    peer=config();peer['name']='compai.local'
    with running(peer,tmp_path) as upstream:
        monkeypatch.setattr(web,'PORT',upstream)
        local=config();local['peers']['compai.local']='127.0.0.1'
        with running(local,tmp_path/'missing-local-assets') as port:
            status,headers,body=read(port,'/pipeline.html',host='compai.jswarm.localhost')
            assert status==200 and body==b'peer packaged pipeline'
            assert headers['X-JSwarm-Node']=='compai.local'
            assert json.loads(read(port,'/api/jswarm-web',host='compai.jswarm.localhost')[2])['node']=='compai.local'


def test_unknown_machine_does_not_fall_back_to_coordinator(tmp_path,monkeypatch):
    monkeypatch.setattr(web,'PORT',1)
    local=config();local['peers']['compai.local']='127.0.0.1'
    with running(local,tmp_path) as port:
        assert read(port,'/pipeline.html',host='compai.jswarm.localhost')[0]==502


def test_control_proxy_preserves_csrf_gate_and_rewrites_only_valid_origins(tmp_path,monkeypatch):
    requests=[]
    class Upstream(web.BaseHTTPRequestHandler):
        def do_POST(self):
            requests.append((dict(self.headers),self.rfile.read(int(self.headers['Content-Length']))))
            body=b'{"accepted":true}'
            self.send_response(200);self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
    server=web.ThreadingHTTPServer(('127.0.0.1',0),Upstream)
    worker=threading.Thread(target=server.serve_forever,daemon=True);worker.start()
    original=http.client.HTTPConnection
    def connection(host,port=None,**kwargs):
        if host=='10.42.0.1' and port==80:return original('127.0.0.1',server.server_port,**kwargs)
        return original(host,port,**kwargs)
    monkeypatch.setattr(web.http.client,'HTTPConnection',connection)
    try:
        with running(config(),tmp_path) as port:
            headers={'Origin':'http://jswarm.localhost','Content-Type':'application/json','X-JSwarm-Control':'1'}
            assert read(port,'/api/tasks/machine/control',method='POST',headers=headers,body='{}')[0]==200
            forwarded,body=requests[0]
            assert forwarded['Host']=='map.local' and forwarded['Origin']=='http://map.local'
            assert forwarded['X-JSwarm-Control']=='1' and body==b'{}'
            assert read(port,'/api/tasks/machine/control',method='POST',headers={**headers,'Origin':'http://compai.jswarm.localhost'},body='{}')[0]==403
            assert len(requests)==1
    finally:server.shutdown();server.server_close();worker.join()


def test_failed_web_replacement_restores_only_its_predecessor(tmp_path,monkeypatch):
    from types import SimpleNamespace
    monkeypatch.setattr(web.Path,'home',lambda:tmp_path)
    image='sha256:'+'a'*64;calls=[]
    monkeypatch.setattr(web.subprocess,'check_output',lambda *a,**k:json.dumps(image))
    current={'Config':{'Labels':{web.LABEL:web.SERVICE}},'State':{'Running':True}}
    def run(args,**kwargs):
        calls.append(args)
        if args[1]=='inspect':return SimpleNamespace(returncode=0,stdout=json.dumps([current]))
        if args[1]=='run':raise web.subprocess.CalledProcessError(1,args)
        return SimpleNamespace(returncode=0,stdout='')
    monkeypatch.setattr(web.subprocess,'run',run)
    with pytest.raises(web.subprocess.CalledProcessError):web.install(image,config())
    assert ['docker','rename','swarm-node-web-previous','swarm-node-web'] in calls
    assert ['docker','start','swarm-node-web'] in calls
    assert all('swarm-node' not in call and 'swarm-control-view' not in call for call in calls)


def test_existing_ipv6_frontend_alias_is_loopback_only_and_idempotent():
    source='http { server { listen [::]:80 ipv6only=on; server_name map.local; } }'
    updated=web.frontend_configuration(source)
    assert source[:-1] in updated
    assert 'server_name jswarm.localhost *.jswarm.localhost;' in updated
    assert 'allow ::1;' in updated and 'deny all;' in updated
    assert web.frontend_configuration(updated)==updated
    with pytest.raises(ValueError):web.frontend_configuration('http { server { server_name other; } }')
