"""Typed cache RPCs fence machine/owner identity and Docker references."""
import asyncio
import base64
import hashlib
import json
from pathlib import Path
import secrets
import threading
import time
from types import SimpleNamespace

import pytest

from scripts import launcher_agent as node

KEY = 'a' * 64
OWNER = 'b' * 64
MACHINE = hashlib.sha256(b'fixture-machine').hexdigest()
NAME = 'swarm-job-' + KEY[:32]
VOLUME = 'swarm-cache-' + KEY[:32]


class Docker:
    def __init__(self):
        self.owner = {'Id': OWNER, 'Name': '/' + NAME, 'Created': '2026-11-01', 'ExecIDs': [],
                      'Config': {'Labels': {'org.lifetech.cache-owner': KEY, 'org.lifetech.group': 'shared-cache'}},
                      'State': {'Status': 'running'},
                      'Mounts': [{'Type': 'volume', 'Name': VOLUME, 'Destination': '/dev/shm'}]}
        self.volume = {'Name': VOLUME, 'Driver': 'local', 'Options': {'type': 'tmpfs', 'device': 'tmpfs'},
                       'Labels': {'org.lifetech.cache-owner': KEY}}
        self.readers = []
        self.calls = []
        self.mutations = []
        self.attach_on_remove = False

    def __call__(self, argv, **kwargs):
        args = argv[1:];self.calls.append(args)
        result = lambda value: SimpleNamespace(returncode=0, stdout=value, stderr='')
        missing = lambda: SimpleNamespace(returncode=1, stdout='', stderr='No such object')
        if args[:2] == ['ps', '-aq']:
            return result(OWNER if self.owner else '')
        if args[0] == 'inspect':
            return result(json.dumps([self.owner] if self.owner else []))
        if args[:2] == ['container', 'inspect']:
            return result(json.dumps([self.owner])) if self.owner else missing()
        if args[:2] == ['volume', 'ls']:
            return result(VOLUME if self.volume else '')
        if args[:2] == ['volume', 'inspect']:
            return result(json.dumps([self.volume])) if self.volume else missing()
        if args[:2] == ['ps', '-a']:
            return result('\n'.join(([NAME] if self.owner else []) + self.readers))
        if args[:2] == ['rm', '-f']:
            assert args[2] == self.owner['Id']
            self.mutations.append(args);self.owner = None
            return result('removed')
        if args[:2] == ['volume', 'rm']:
            assert args == ['volume', 'rm', VOLUME]  # Never force or prune.
            if self.attach_on_remove or self.readers or self.owner:
                return SimpleNamespace(returncode=1, stdout='', stderr='volume is in use')
            self.mutations.append(args);self.volume = None
            return result('removed')
        pytest.fail('Unexpected Docker operation: ' + repr(args))


@pytest.fixture
def docker(monkeypatch):
    fake = Docker()
    monkeypatch.setattr(node.subprocess, 'run', fake)
    original = Path.read_bytes
    monkeypatch.setattr(Path, 'read_bytes', lambda path: b'fixture-machine' if str(path) == '/etc/machine-id' else original(path))
    return fake


def request(**changes):
    return {'key': KEY, 'owner_id': OWNER, 'machine': MACHINE, 'ownership': 'terminal-only', 'dry_run': False, **changes}


def test_cleanup_inspection_and_dry_run_do_not_mutate(docker):
    agent = object.__new__(node.Agent)
    inventory = agent.dispatch('resource.caches.inspect', {})
    assert inventory['items'][0]['owner_id'] == OWNER
    assert inventory['items'][0]['volume_verified']
    result = agent.dispatch('resource.caches.cleanup', request(dry_run=True))
    assert result['reason'] == 'eligible_terminal_cache' and not result['removed']
    assert docker.mutations == []


def test_cleanup_removes_exact_owner_then_nonforced_volume(docker):
    result = node.cleanup_ram_cache(request())
    assert result['removed'] and result['owner_removed']
    assert docker.mutations == [['rm', '-f', OWNER], ['volume', 'rm', VOLUME]]


@pytest.mark.parametrize('change', [
    {'key': '../unsafe'}, {'owner_id': 'wrong'}, {'machine': 'wrong'},
    {'ownership': 'anything'}, {'dry_run': 1}, {'extra': True},
])
def test_invalid_arguments_are_rejected_before_docker(docker, change):
    with pytest.raises(ValueError):
        node.cleanup_ram_cache(request(**change))
    assert not docker.calls


def test_different_machine_is_rejected_before_docker(docker):
    with pytest.raises(ValueError, match='machine identity'):
        node.cleanup_ram_cache(request(machine='c' * 64))
    assert not docker.calls


@pytest.mark.parametrize('condition', ['replacement', 'consumer', 'exec', 'foreign-volume', 'wrong-group', 'wrong-mount'])
def test_cleanup_preserves_consumers_and_mismatched_objects(docker, condition):
    if condition == 'replacement':docker.owner['Id'] = 'c' * 64
    elif condition == 'consumer':docker.readers = ['stopped-but-attached-scientific-job']
    elif condition == 'exec':docker.owner['ExecIDs'] = ['bulk-transfer']
    elif condition == 'foreign-volume':docker.volume['Driver'] = 'foreign'
    elif condition == 'wrong-group':docker.owner['Config']['Labels']['org.lifetech.group'] = 'compute'
    elif condition == 'wrong-mount':docker.owner['Mounts'] = []
    assert not node.cleanup_ram_cache(request())['removed']
    assert not docker.mutations


def test_ownerless_volume_cleanup_never_deletes_a_new_owner(docker):
    assert not node.cleanup_ram_cache(request(owner_id=None))['removed']
    assert not docker.mutations
    docker.owner = None
    assert node.cleanup_ram_cache(request(owner_id=None))['removed']
    assert docker.mutations == [['volume', 'rm', VOLUME]]


def test_new_attachment_wins_the_orphan_volume_removal_race(docker):
    docker.owner = None;docker.attach_on_remove = True
    result = node.cleanup_ram_cache(request(owner_id=None))
    assert not result['removed'] and result['reason'] == 'docker_consumer'
    assert not docker.mutations


@pytest.mark.parametrize('operation', ['resource.caches.inspect', 'resource.caches.cleanup'])
def test_cache_maintenance_requires_the_controller_signature(operation):
    with pytest.raises(ValueError, match='signature rejected'):
        node.verify_controller({'from': 'master', 'op': operation}, {'master': 'master', 'controller_public_key': '00' * 32})


def test_real_encrypted_signed_rpc_routes_to_typed_cleanup(tmp_path, docker):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    key = Ed25519PrivateKey.generate()
    public = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    config = tmp_path / 'node.json'
    config.write_text(json.dumps({'name': 'worker', 'master': 'master', 'key': 'ab' * 32,
                                 'peers': {'master': 'address'}, 'controller_public_key': public.hex()}))
    agent = object.__new__(node.Agent);agent.config = config;agent.nonces = {};agent.nonce_lock = threading.Lock()
    cipher = AESGCM(bytes.fromhex('ab' * 32))
    value = {'from': 'master', 'to': 'worker', 'time': time.time(), 'nonce': secrets.token_hex(16),
             'op': 'resource.caches.cleanup', 'args': request(dry_run=True)}
    value['controller_signature'] = base64.b64encode(key.sign(json.dumps(value, sort_keys=True, separators=(',', ':')).encode())).decode()
    nonce = secrets.token_bytes(12)
    reply = asyncio.run(agent.process(nonce + cipher.encrypt(nonce, json.dumps(value).encode(), node.VERSION.encode())))
    response = json.loads(cipher.decrypt(reply[:12], reply[12:], node.VERSION.encode()))
    assert response['ok'] and response['result']['reason'] == 'eligible_terminal_cache'
    assert not docker.mutations
