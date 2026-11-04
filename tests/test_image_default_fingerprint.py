import json
from types import SimpleNamespace
from unittest.mock import patch
from docker_utils.images import ImageResolver


def fingerprint(config):
    metadata={'RootFS':{'Type':'layers','Layers':['sha256:unchanged']},'Config':config,'Architecture':'amd64','Os':'linux','Created':'2026-11-03T00:00:00Z'}
    resolver=ImageResolver([],lambda h:[])
    with patch.object(resolver,'inspect',return_value='sha256:'+'1'*64),patch('docker_utils.images.subprocess.run',return_value=SimpleNamespace(returncode=0,stdout=json.dumps([metadata]))):
        return resolver.fingerprint(None,'tools')


def test_docker_client_omitted_defaults_preserve_image_identity():
    assert fingerprint({'Cmd':['bwa'],'User':None,'AttachStdin':None})==fingerprint({'Cmd':['bwa'],'User':'','AttachStdin':False,'AttachStdout':False,'AttachStderr':False,'Tty':False,'StdinOnce':False,'OpenStdin':False,'WorkingDir':'','Image':'','Domainname':'','Hostname':''})


def test_real_runtime_configuration_difference_is_not_normalized():
    assert fingerprint({'Cmd':['bwa'],'User':''})!=fingerprint({'Cmd':['bwa'],'User':'1000'})
    assert fingerprint({'Cmd':['bwa']})!=fingerprint({'Cmd':['another-aligner']})


def test_agent_and_coordinator_use_the_same_image_identity():
    from scripts.launcher_agent import Agent
    metadata={'RootFS':{'Type':'layers','Layers':['sha256:unchanged']},'Config':{'Cmd':['bwa'],'User':None},'Architecture':'amd64','Os':'linux','Created':'2026-11-03T00:00:00Z'}
    assert Agent.image_fingerprint(SimpleNamespace(attrs=metadata))==fingerprint(metadata['Config'])
