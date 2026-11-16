"""Repo bind mounts, service isolation and actual host-visible container FUSE."""
import json
import mmap
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import pytest
from scripts.swarm_python_container import command,unit

IMAGE='organization-python-services:20261115'
ROOT=Path(__file__).resolve().parents[2]


def options(**changes):
    return dict(workspace=str(ROOT),home=str(Path.home()),image='sha256:'+'a'*64,
                name='swarm-python-fixture',script='JSwarm/scripts/swarm_fs_mount.py',**changes)


def test_readonly_repos_and_pinned_runtime():
    args=command(**options())
    assert args[:4]==['/usr/bin/docker','--host','unix:///var/run/docker.sock','run']
    assert f'type=bind,src={ROOT},dst={ROOT},readonly' in args
    assert 'no-new-privileges' in args
    assert '--privileged' not in args and '/var/run/docker.sock' not in args
    assert 'PYTHONPATH='+str(ROOT) in args
    with pytest.raises(ValueError):command(**{**options(),'image':'python:latest'})
    with pytest.raises(ValueError):command(**{**options(),'script':'../outside.py'})


def test_rootless_fuse_is_rejected_and_mount_helper_is_bounded():
    with pytest.raises(ValueError,match='rootless'):
        command(**options(rootless=True,fuse_mount='/tmp/fixture'))
    args=command(**options(fuse_mount='/tmp/fixture'))
    assert '--device' in args and '/dev/fuse' in args and 'SYS_ADMIN' in args
    assert 'no-new-privileges' not in args
    assert 'type=bind,src=/tmp/fixture,dst=/tmp/fixture,bind-propagation=rshared' in args


def test_unit_escapes_systemd_specifiers():
    text=unit(**{**options(),'workspace':'/tmp/repo % here'})
    assert '/tmp/repo %% here' in text and 'ExecStop=' in text


@pytest.mark.parametrize('major',['2','3'])
def test_container_mount_is_visible_and_mappable_on_host(tmp_path,major):
    from scripts.swarm_filesystem import version
    result=subprocess.run(['docker','image','inspect',IMAGE,'--format','{{.Id}}'],capture_output=True,text=True)
    if result.returncode:pytest.skip('Build Dockerfile.python-services first')
    image=result.stdout.strip();source=tmp_path/'origin';source.mkdir();reference=source/'reference'
    data=bytes(range(256))*256;reference.write_bytes(data);reference.chmod(0o444)
    (source/'ordinary').write_bytes(b'old');mount=tmp_path/'mount';mount.mkdir()
    config=tmp_path/'config.json';config.write_text(json.dumps({'namespace':'container-fixture','origin':'fixture.local','secret':'ab'*32,
        'root':str(source),'peers':[],'minimum_free_bytes':0,'fuse_version':major,'immutable_files':{'/reference':version(reference.stat())}}))
    fixture_root=ROOT/'.local-reports';fixture_root.mkdir(exist_ok=True)
    from test_filesystem_mmap_host import CHILD
    with tempfile.TemporaryDirectory(prefix='container-fuse-',dir=fixture_root) as folder:
        script=Path(folder)/'mount.py';script.write_text(CHILD)
        opts=options(arguments=['/unused',str(ROOT/'JSwarm'),str(config),str(mount)],writable=[str(tmp_path)],fuse_mount=str(mount))
        opts.update(image=image,script=str(script.relative_to(ROOT)),name='swarm-python-test-'+str(os.getpid())+'-'+major,uid=os.getuid(),gid=os.getgid())
        args=command(**opts)
        with (tmp_path/'container.log').open('w+') as log:
            process=subprocess.Popen(args,stdout=log,stderr=log)
            try:
                deadline=time.monotonic()+20
                while not os.path.ismount(mount) and process.poll() is None and time.monotonic()<deadline:time.sleep(.05)
                log.flush();log.seek(0);assert os.path.ismount(mount),log.read()
                with (mount/'reference').open('rb') as stream:
                    with mmap.mmap(stream.fileno(),0,access=mmap.ACCESS_READ) as view:assert view[4096:8192]==data[4096:8192]
                with (mount/'ordinary').open('r+b',buffering=0) as stream:stream.write(b'new')
                assert (source/'ordinary').read_bytes()==b'new'
            finally:
                subprocess.run(['docker','stop','--time','3',opts['name']],capture_output=True,timeout=15)
                if process.poll() is None:process.terminate()
                process.wait(timeout=10)
                if os.path.ismount(mount):subprocess.run(['fusermount3','-u',str(mount)],capture_output=True,timeout=10)
