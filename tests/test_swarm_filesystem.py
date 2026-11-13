import base64
import errno
import json
import os
from pathlib import Path
from unittest.mock import Mock
import pytest
from scripts.run_input_cache import InputCache
from scripts.swarm_filesystem import Filesystem, request, BLOCK
from scripts.swarm_fs_mount import Client


@pytest.mark.parametrize('requested,available,expected', [
    ('auto', {'fuse3': 'libfuse3.so.3', 'fuse': 'libfuse.so.2'}, 'libfuse3.so.3'),
    ('auto', {'fuse': 'libfuse.so.2'}, 'libfuse.so.2'),
    ('2', {'fuse3': 'libfuse3.so.3', 'fuse': 'libfuse.so.2'}, 'libfuse.so.2'),
    ('3', {'fuse3': 'libfuse3.so.3'}, 'libfuse3.so.3'),
])
def test_selects_system_fuse_major(requested, available, expected, monkeypatch):
    from scripts.swarm_fs_mount import select_system_fuse
    import ctypes.util
    monkeypatch.setattr(ctypes.util, 'find_library', available.get)
    monkeypatch.setenv('FUSE_LIBRARY_PATH', '')
    assert select_system_fuse({'fuse_version': requested}) == expected
    assert os.environ['FUSE_LIBRARY_PATH'] == expected


def test_missing_requested_fuse_version_fails_without_bundled_library(monkeypatch):
    from scripts.swarm_fs_mount import select_system_fuse
    import ctypes.util
    monkeypatch.setattr(ctypes.util, 'find_library', lambda name: 'libfuse.so.2' if name == 'fuse' else None)
    with pytest.raises(OSError):select_system_fuse({'fuse_version': '3'})


@pytest.fixture
def fs(tmp_path):
    root = tmp_path/'data';root.mkdir()
    settings = {'namespace': 'test', 'secret': 'ab'*32, 'origin': 'latitude.local',
                'root': str(root), 'minimum_free_bytes': 0, 'peers': ['peer.local']}
    config = tmp_path/'filesystem.json';config.write_text(json.dumps(settings))
    service = Filesystem(config, InputCache(tmp_path/'ram', budget=2*BLOCK, reserve=0))
    def call(op, **args):
        row = service.dispatch(request(settings, op, **args))
        if 'errno' in row:raise OSError(row['errno'], 'filesystem')
        return row['result']
    return root, settings, service, call


def test_durable_write_stale_writer_and_external_mutation(fs):
    root, settings, service, call = fs
    old = call('create', path='/sample')
    new = call('write', path='/sample', version=old['version'], offset=0, data=base64.b64encode(b'abc').decode())
    assert (root/'sample').read_bytes() == b'abc'
    with pytest.raises(OSError) as error:call('write', path='/sample', version=old['version'], offset=0, data='eA==')
    assert error.value.errno == errno.ESTALE
    (root/'sample').write_bytes(b'external')
    with pytest.raises(OSError):call('read', path='/sample', version=new['version'], offset=0, length=3)


def test_grant_tampering_replay_and_revocation(fs):
    root, settings, service, call = fs
    value = request(settings, 'status')
    service.dispatch(value)
    with pytest.raises(PermissionError):service.dispatch(value)
    value = request(settings, 'status');value['operation'] = 'unlink'
    with pytest.raises(PermissionError):service.dispatch(value)
    value = request(settings, 'status')
    service.config.write_text(json.dumps({**settings, 'secret': 'cd'*32}))
    with pytest.raises(PermissionError):service.dispatch(value)


@pytest.mark.parametrize('path', ['/../escape', '/link/escape', '/leaf'])
def test_traversal_and_symlinks_cannot_escape(fs, path, tmp_path):
    root, settings, service, call = fs
    outside = tmp_path/'outside';outside.mkdir();(outside/'escape').write_bytes(b'secret')
    (root/'link').symlink_to(outside, target_is_directory=True)
    (root/'leaf').symlink_to(outside/'escape')
    with pytest.raises(OSError):call('read', path=path, version='0'*64, offset=0, length=6)
    assert (outside/'escape').read_bytes() == b'secret'


def test_mutations_and_reserve(fs):
    root, settings, service, call = fs
    assert call('statfs')['f_bavail'] > 0
    call('mkdir', path='/folder');row = call('create', path='/folder/file')
    call('truncate', path='/folder/file', version=row['version'], size=10)
    call('rename', path='/folder/file', destination='/file')
    assert call('list', path='/')['names'] == ['file', 'folder']
    call('rmdir', path='/folder')
    settings['minimum_free_bytes'] = 2**63;service.config.write_text(json.dumps(settings))
    row = call('stat', path='/file')
    with pytest.raises(OSError) as error:call('write', path='/file', version=row['version'], offset=10, data='YQ==')
    assert error.value.errno == errno.ENOSPC
    call('unlink', path='/file')


def test_peer_cache_local_cache_and_invalidation(fs, tmp_path, monkeypatch):
    root, settings, service, call = fs
    data = b'x'*(BLOCK+7);(root/'reads').write_bytes(data)
    peer_config = tmp_path/'peer.json';peer_config.write_text(json.dumps({k:v for k,v in settings.items() if k != 'root'}))
    peer = Filesystem(peer_config, InputCache(tmp_path/'peer-ram', budget=4*BLOCK, reserve=0))
    monkeypatch.setattr('scripts.launcher_discovery.resolve_endpoint', lambda n: ('http://'+n+':8790', ['127.0.0.1']))
    class Agent:
        def __init__(self, host):self.node = host['host']
        def call(self, op, value, **kwargs):
            assert op == 'filesystem.rpc'
            return (service if self.node == settings['origin'] else peer).dispatch(value)
    one = Client(settings, factory=Agent, cache=InputCache(tmp_path/'one', reserve=0))
    two = Client(settings, factory=Agent, cache=InputCache(tmp_path/'two', reserve=0))
    assert one.read('/reads', len(data), 0) == data
    assert one.counters['origin_reads'] == 2
    assert one.read('/reads', 7, BLOCK) == b'x'*7
    assert one.counters['local_hits'] == 1
    assert two.read('/reads', BLOCK, 0) == b'x'*BLOCK
    assert two.counters['peer_hits'] == 1
    assert two.write('/reads', b'NEW', 0) == 3
    assert one.read('/reads', 3, 0) == b'NEW'


def test_complete_write_primes_only_committed_local_cache(fs, tmp_path, monkeypatch):
    root, settings, service, call = fs
    (root/'output').touch()
    client = Client(settings, cache=InputCache(tmp_path/'local', reserve=0))
    client.origin = call
    assert client.write('/output', b'committed', 0) == 9
    assert client.read('/output', 9, 0) == b'committed'
    assert client.counters['local_hits'] == 1


def test_publication_embeds_filesystem_and_ram_cache():
    from agent_updates import LauncherUpdates
    source = LauncherUpdates(Path(__file__).parents[1], lambda: False, Mock())
    namespace = {'__name__': 'bundled_filesystem'}
    agent = source.files['launcher_agent.py']
    import ast
    tree = ast.parse(agent)
    values = [node.value.value for node in tree.body if isinstance(node, ast.Assign)
              and any(isinstance(t, ast.Name) and t.id == 'FILESYSTEM_RUNTIME_SOURCE' for t in node.targets)
              and isinstance(node.value, ast.Constant)]
    exec(values[-1], namespace)
    assert namespace['CACHE_SOURCE'].startswith('"""Private bounded block cache')
    assert 'Filesystem' in namespace


def test_host_mount_routes_through_local_node_without_host_crypto(fs, tmp_path, monkeypatch):
    import socketserver
    import threading
    import tempfile
    import shutil
    from scripts.swarm_filesystem import canonical
    root, settings, service, call = fs
    home = Path(tempfile.mkdtemp(prefix='swfs-'));config = home/'.config/lifetech-launcher';config.mkdir(parents=True)
    settings.update(client_release=str(Path(__file__).parents[1]), local_gateway=True)
    service.config.write_text(json.dumps(settings))
    (root/'network').write_bytes(b'portable host adapter')
    class Agent:
        def __init__(self, host):pass
        def call(self, op, value, **kwargs):return service.dispatch(value)
    monkeypatch.setattr('agent_client.AgentClient', Agent)
    monkeypatch.setattr('scripts.launcher_discovery.resolve_endpoint', lambda n: ('http://'+n+':8790', ['127.0.0.1']))
    monkeypatch.setattr(Path, 'home', lambda: home)
    class Handler(socketserver.StreamRequestHandler):
        def handle(self):
            value = json.loads(self.rfile.readline())
            self.wfile.write(canonical({'ok': True, 'result': service.dispatch(value)})+b'\n')
    server = socketserver.ThreadingUnixStreamServer(str(config/'filesystem.sock'), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True);thread.start()
    try:
        client = Client(settings, cache=InputCache(tmp_path/'host-ram', reserve=0))
        assert client.read('/network', 100, 0) == b'portable host adapter'
        assert client.write('/network', b'NEW', 0) == 3
        assert (root/'network').read_bytes().startswith(b'NEW')
    finally:server.shutdown();server.server_close();thread.join();shutil.rmtree(home)
