import asyncio
import json
from types import SimpleNamespace

import pytest
from scripts import launcher_peers as peers


def configuration():
    return peers.pipeline_configuration({'name':'codecollective.local','peers':{'codecollective.local':'10.42.0.49','compai.local':'10.42.0.1','julian-minipc.local':'10.42.0.221'}})


def test_native_aliases_reuse_existing_node_endpoints():
    config=configuration()
    assert peers.pipeline_target(config,'127.0.0.1:8791')=='codecollective.local'
    assert peers.pipeline_target(config,'jswarm.localhost')=='codecollective.local'
    assert peers.pipeline_target(config,'compai.jswarm.localhost')=='compai.local'
    assert peers.pipeline_target(config,'minipc.jswarm.localhost')=='julian-minipc.local'
    assert peers.pipeline_target(config,'compai.local:8790')=='compai.local'
    for host in ('evil.test','compai.jswarm.localhost.evil','user@compai.jswarm.localhost','compai.local:8797'):
        with pytest.raises(ValueError):peers.pipeline_target(config,host)


def test_native_pipeline_assets_contain_no_private_node_configuration():
    assets=peers.pipeline_assets()
    assert 'pipeline.html' in assets and 'node-web.js' in assets
    assert 'genome/genome-explorer.html' in assets
    assert not any(name.endswith(('.key','.pem')) or name.startswith('../') for name in assets)


def test_native_http_routes_serve_pipeline_and_enforce_origin(tmp_path,monkeypatch):
    pytest.importorskip('aiohttp')
    from aiohttp import web
    from aiohttp.test_utils import TestClient,TestServer
    path=tmp_path/'peers.json';path.write_text(json.dumps({'name':'codecollective.local','peers':configuration()['peers']}))
    monkeypatch.setattr(peers,'pipeline_assets',lambda:{'pipeline.html':b'<main>native node pipeline</main>'})
    async def exercise():
        app=web.Application();app.router.add_route('*','/{path:.*}',peers.NodePipelineWeb(SimpleNamespace(config=path)).handle)
        async with TestClient(TestServer(app)) as client:
            response=await client.get('/pipeline.html',headers={'Host':'jswarm.localhost'})
            assert response.status==200 and await response.text()=='<main>native node pipeline</main>'
            assert response.headers['X-JSwarm-Node']=='codecollective.local'
            response=await client.get('/api/jswarm-web',headers={'Host':'jswarm.localhost'})
            body=await response.json()
            assert body['node']=='codecollective.local'
            assert body['nodes'][1]['direct_url']=='http://10.42.0.1/pipeline.html'
            assert 'key' not in body
            response=await client.get('/pipeline.html',headers={'Host':'evil.test'})
            assert response.status==421
            response=await client.post('/api/tasks/machine/control',headers={'Host':'jswarm.localhost','Origin':'http://evil.test'})
            assert response.status==403
            response=await client.post('/api/tasks/machine/control',headers={'Host':'jswarm.localhost'})
            assert response.status==403
    asyncio.run(exercise())


def test_pipeline_assets_are_bundled_into_the_standard_node_revision():
    import base64,gzip,sys
    from pathlib import Path
    sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'swarm-control/server'))
    from agent_updates import LauncherUpdates
    release=LauncherUpdates(Path(__file__).resolve().parents[2],lambda:False,lambda *args:None)
    import ast
    tree=ast.parse(release.files['launcher_peers.py'])
    value=next(ast.literal_eval(node.value) for node in tree.body if isinstance(node,ast.Assign)
               and any(isinstance(target,ast.Name) and target.id=='PIPELINE_ASSETS_B64' for target in node.targets))
    assets=json.loads(gzip.decompress(base64.b64decode(value)))
    assert b'Pipeline' in base64.b64decode(assets['pipeline.html'])
    assert 'node-web.js' in assets
    assert release.files['launcher_peers.py'].index('PIPELINE_ASSETS_B64 =')<release.files['launcher_peers.py'].index("if __name__=='__main__':main()")


def test_native_api_routing_does_not_depend_on_the_retired_nginx_view(tmp_path,monkeypatch):
    aiohttp=pytest.importorskip('aiohttp')
    from aiohttp import web
    from aiohttp.test_utils import TestClient,TestServer
    path=tmp_path/'peers.json';settings={'name':'codecollective.local','peers':configuration()['peers']};path.write_text(json.dumps(settings))
    calls=[]
    class Upstream:
        status=200;headers={'Content-Type':'application/json'}
        def __init__(self):self.content=self
        async def __aenter__(self):return self
        async def __aexit__(self,*args):pass
        async def iter_chunked(self,size):yield b'{"ok":true}'
    class Session:
        def __init__(self,**kwargs):pass
        async def __aenter__(self):return self
        async def __aexit__(self,*args):pass
        def request(self,method,url,**kwargs):calls.append((method,url,kwargs['headers']));return Upstream()
    async def exercise():
        app=web.Application();app.router.add_route('*','/{path:.*}',peers.NodePipelineWeb(SimpleNamespace(config=path)).handle)
        async with TestClient(TestServer(app)) as client:
            monkeypatch.setattr(aiohttp,'ClientSession',Session)
            response=await client.get('/api/tasks/pipeline',headers={'Host':'jswarm.localhost'})
            assert await response.json()=={'ok':True}
            assert calls[-1][1]=='http://10.42.0.1:8790/api/tasks/pipeline'
            settings['name']='compai.local';path.write_text(json.dumps(settings))
            for route,expected in [('/api/tasks/pipeline',8792),('/api/resources',8795),('/api/health',8089)]:
                response=await client.get(route,headers={'Host':'jswarm.localhost'});await response.read()
                assert calls[-1][1]=='http://127.0.0.1:'+str(expected)+route
            response=await client.post('/api/tasks/machine/control',json={'host':'compai.local','action':'pause'},headers={'Host':'jswarm.localhost','Origin':'http://jswarm.localhost','X-JSwarm-Control':'1'})
            await response.read()
            assert calls[-1][2]['Host']=='map.local' and calls[-1][2]['Origin']=='http://map.local'
            assert calls[-1][2]['X-JSwarm-Control']=='1'
            response=await client.get('/api/v1/master/codex/threads',headers={'Host':'jswarm.localhost'})
            assert response.status==403
    asyncio.run(exercise())
