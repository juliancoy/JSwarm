import shlex
import pytest
from scripts.agent_container import service


def unit(**overrides):
    values=dict(source='/home/owner/agent/scripts',config='/home/owner/.config/lifetech-launcher/peers.json',
                jobs='/home/owner/jobs',socket='/var/run/docker.sock',image='sha256:'+'a'*64,
                uid=1000,gid=1000,socket_gid=137,hostname='fixture')
    return service(**(values|overrides))


def test_control_and_worker_daemons_stay_separate():
    text=unit(worker_socket='/run/user/1000/docker.sock')
    args=shlex.split(next(line.removeprefix('ExecStart=') for line in text.splitlines() if line.startswith('ExecStart=')))
    assert args[:3]==['/usr/bin/env','DOCKER_HOST=unix:///var/run/docker.sock','/usr/bin/docker']
    environment=[args[i+1] for i,value in enumerate(args) if value=='--env']
    mounts=[args[i+1] for i,value in enumerate(args) if value=='--mount']
    assert 'DOCKER_HOST=unix:///run/user/1000/docker.sock' in environment
    assert any('src=/run/user/1000/docker.sock,dst=/run/user/1000/docker.sock' in mount for mount in mounts)
    assert not any('src=/var/run/docker.sock,' in mount for mount in mounts)
    assert 'ExecStop=/usr/bin/env DOCKER_HOST=unix:///var/run/docker.sock /usr/bin/docker stop' in text
    assert 'LIFETECH_JOB_ROOT=/home/owner/jobs' in environment
    assert '--entrypoint' in args and args[args.index('--entrypoint')+1]=='python'


@pytest.mark.parametrize('overrides',[{'socket':'/run/user/1000/docker.sock'},{'image':'python:latest'},{'jobs':'relative/jobs'}])
def test_unverified_runtime_and_network_changes_are_rejected(overrides):
    with pytest.raises(ValueError):unit(**overrides)


def test_standard_service_mounts_source_and_runs_supervisor():
    text=unit()
    args=shlex.split(next(line.removeprefix('ExecStart=') for line in text.splitlines() if line.startswith('ExecStart=')))
    assert args[-1]=='/home/owner/agent/scripts/launcher_peer_supervisor.py'
    assert args[args.index('--name')+1]=='swarm-node'
    assert 'type=bind,src=/home/owner/agent/scripts,dst=/home/owner/agent/scripts,readonly' in args
    assert unit().find('/home/owner/agent/scripts/launcher_peer_supervisor.py')>=0


def test_cpu_reserve_defaults_to_one_and_can_be_disabled():
    assert 'SWARM_RESERVED_CPUS=1' in unit()
    assert 'SWARM_RESERVED_CPUS=0' in unit(reserved_cpus=0)
    assert 'SWARM_RESERVED_CPUS=3' in unit(reserved_cpus=3)
    for value in (-1,1.5,True):
        with pytest.raises(ValueError,match='nonnegative integer'):unit(reserved_cpus=value)
