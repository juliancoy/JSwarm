import asyncio
import json
from types import SimpleNamespace

import pytest
from scripts import launcher_peers as peers


@pytest.fixture(autouse=True)
def isolated_web_workspace(monkeypatch):
    monkeypatch.delenv("SWARM_WORKSPACE",raising=False)
    monkeypatch.delenv("SWARM_WEB_WORKSPACE",raising=False)


def configuration():
    return peers.pipeline_configuration({'name':'codecollective.local','peers':{'codecollective.local':'10.42.0.49','compai.local':'10.42.0.1','julian-minipc.local':'10.42.0.221'}})


def test_native_aliases_reuse_existing_node_endpoints():
    config=configuration()
    assert peers.pipeline_target(config,'127.0.0.1:8791')=='codecollective.local'
    assert peers.pipeline_target(config,'jswarm.localhost')=='codecollective.local'
    assert peers.pipeline_target(config,'compai.jswarm.localhost')=='compai.local'
    assert peers.pipeline_target(config,'minipc.jswarm.localhost')=='julian-minipc.local'
    assert peers.pipeline_target(config,'compai.local:8790')=='compai.local'
    assert peers.pipeline_target(config,'compai-swarm.local:8790')=='compai.local'
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
    monkeypatch.setattr(peers,'pipeline_assets',lambda:{
        'pipeline.html':b'<html><head></head><body><main>native node pipeline</main></body></html>',
        'index.html':b'<html><head></head><body>separate network map</body></html>'})
    async def exercise():
        app=web.Application();app.router.add_route('*','/{path:.*}',peers.NodePipelineWeb(SimpleNamespace(config=path)).handle)
        async with TestClient(TestServer(app)) as client:
            response=await client.get('/pipeline.html',headers={'Host':'jswarm.localhost'})
            body=await response.text()
            assert response.status==200 and '<main>native node pipeline</main>' in body
            assert body.count('src="/swarm-navigation.js"')==1
            assert response.headers['X-JSwarm-Node']=='codecollective.local'
            assert 'no-store' in response.headers['Cache-Control'] and response.headers['Pragma']=='no-cache'
            response=await client.get('/index.html',headers={'Host':'localhost'})
            body=await response.text()
            assert response.status==200 and 'separate network map' in body
            assert body.count('src="/swarm-navigation.js"')==1
            response=await client.get('/api/jswarm-web',headers={'Host':'jswarm.localhost'})
            body=await response.json()
            assert body['node']=='codecollective.local'
            assert body['nodes'][1]['direct_url']=='http://10.42.0.1/pipeline.html'
            assert 'key' not in body
            response=await client.get('/pipeline.html',headers={'Host':'codecollective-swarm.local:8790'})
            assert response.status==200
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
    monkeypatch.setattr('launcher_discovery.resolve_endpoint',lambda node:('http://'+node[:-6]+'-swarm.local:8790',['10.42.0.1']))
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
            assert calls[-1][1]=='http://compai-swarm.local:8790/api/tasks/pipeline'
            settings['name']='compai.local';path.write_text(json.dumps(settings))
            for route,expected in [('/api/tasks/pipeline',8792),('/api/tasks/genome-datasets',8792),('/api/v1/swarm/genome-results',8091),('/api/resources',8795),('/api/health',8089)]:
                response=await client.get(route,headers={'Host':'jswarm.localhost'});await response.read()
                assert calls[-1][1]=='http://127.0.0.1:'+str(expected)+route
            response=await client.post('/api/tasks/machine/control',json={'host':'compai.local','action':'pause'},headers={'Host':'jswarm.localhost','Origin':'http://jswarm.localhost','X-JSwarm-Control':'1'})
            await response.read()
            assert calls[-1][2]['Host']=='map.local' and calls[-1][2]['Origin']=='http://map.local'
            assert calls[-1][2]['X-JSwarm-Control']=='1'
            response=await client.get('/api/v1/master/codex/threads',headers={'Host':'jswarm.localhost'})
            assert response.status==403
    asyncio.run(exercise())


def test_workspace_web_reads_each_request_and_constrains_symlinks(tmp_path,monkeypatch):
    pytest.importorskip('aiohttp')
    from aiohttp import web
    from aiohttp.test_utils import TestClient,TestServer
    workspace=tmp_path/'workspace'
    view=workspace/'JSwarm/swarm-control/server/view';view.mkdir(parents=True)
    genome=workspace/'GenomeHarbor/web';genome.mkdir(parents=True)
    asset=genome/'genome-explorer.html';asset.write_text('first edit')
    (view/'genome-explorer.html').symlink_to(asset)
    private=workspace/'private.json';private.write_text('private result')
    (view/'escape.json').symlink_to(private)
    config=tmp_path/'peers.json';config.write_text(json.dumps({'name':'codecollective.local','peers':configuration()['peers'],'web_workspace':str(workspace)}))
    monkeypatch.setattr(peers,'pipeline_assets',lambda:pytest.fail('Source mode must not load bundled bytes'))
    async def exercise():
        app=web.Application();app.router.add_route('*','/{path:.*}',peers.NodePipelineWeb(SimpleNamespace(config=config)).handle)
        async with TestClient(TestServer(app)) as client:
            async def get(name):return await client.get('/'+name,headers={'Host':'localhost'})
            response=await get('genome-explorer.html');assert await response.text()=='first edit'
            assert 'no-store' in response.headers['Cache-Control']
            asset.write_text('second edit without a restart')
            response=await get('genome-explorer.html');assert await response.text()=='second edit without a restart'
            asset.unlink();assert (await get('genome-explorer.html')).status==404
            assert (await get('escape.json')).status==404
            assert (await get('.git/config')).status==404
    asyncio.run(exercise())
    assert peers.pipeline_source_asset(workspace,'../private.json') is None
    assert peers.pipeline_source_asset(workspace,'/private.json') is None


def test_mounted_web_workspace_overrides_bundled_assets(tmp_path,monkeypatch):
    config=tmp_path/'peers.json';config.write_text('{}')
    root=tmp_path/'workspace';(root/'JSwarm/swarm-control/server/view').mkdir(parents=True)
    monkeypatch.setenv('SWARM_WEB_WORKSPACE',str(root))
    monkeypatch.setattr(peers,'PIPELINE_ASSETS_B64','bundled',raising=False)
    assert peers.pipeline_web_root(config)==root
    monkeypatch.setenv('SWARM_WEB_WORKSPACE',str(tmp_path/'missing'))
    with pytest.raises(ValueError,match='mounted Organization'):peers.pipeline_web_root(config)


def test_live_source_revision_tracks_public_edits_and_injects_only_source_html(tmp_path):
    pytest.importorskip('aiohttp')
    from aiohttp import web
    from aiohttp.test_utils import TestClient,TestServer
    workspace=tmp_path/'workspace'
    view=workspace/'JSwarm/swarm-control/server/view';view.mkdir(parents=True)
    genome=workspace/'GenomeHarbor/web';genome.mkdir(parents=True)
    (view/'index.html').write_text('<html><head></head><body>live</body></html>')
    config=tmp_path/'peers.json';config.write_text(json.dumps({'name':'codecollective.local','peers':configuration()['peers'],'web_workspace':str(workspace)}))
    outside=workspace/'private.json';outside.write_text('private')
    (view/'escape.json').symlink_to(outside)
    async def exercise():
        app=web.Application();app.router.add_route('*','/{path:.*}',peers.NodePipelineWeb(SimpleNamespace(config=config)).handle)
        async with TestClient(TestServer(app)) as client:
            async def get(path):return await client.get(path,headers={'Host':'localhost'})
            response=await get('/index.html');assert (await response.text()).count('src="/jswarm-live.js"')==1
            assert response.headers['Pragma']=='no-cache' and response.headers['Expires']=='0'
            assert 'ETag' not in response.headers and 'Last-Modified' not in response.headers
            async def revision():
                response=await get('/api/jswarm-web/source-revision')
                assert 'no-store' in response.headers['Cache-Control']
                body=await response.json();assert set(body)=={'revision'}
                return body['revision']
            initial=await revision()
            outside.write_text('private edit must not trigger public reload')
            assert await revision()==initial
            asset=genome/'data.json';asset.write_text('{"version":1}')
            created=await revision();assert created!=initial
            asset.write_text('{"version":2}')
            edited=await revision();assert edited!=created
            asset.unlink();assert await revision()!=edited
            response=await get('/missing.js');assert response.status==404 and 'no-store' in response.headers['Cache-Control']
            response=await client.get('/api/jswarm-web/source-revision',headers={'Host':'evil.test'})
            assert response.status==421 and 'no-store' in response.headers['Cache-Control']
    asyncio.run(exercise())
