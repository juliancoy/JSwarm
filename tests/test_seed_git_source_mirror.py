"""Real local Git transport checks for private source mirrors; no network access."""
import hashlib
import os
from pathlib import Path
import subprocess
import sys
import pytest
from scripts.seed_git_source_mirror import install_code


def git(folder, *args):
    return subprocess.check_output(['git','-C',str(folder),*args],text=True).strip()


@pytest.fixture
def repository(tmp_path):
    folder=tmp_path/'source';folder.mkdir()
    subprocess.run(['git','init','-q',str(folder)],check=True)
    git(folder,'config','user.name','Mirror fixture')
    git(folder,'config','user.email','fixture@example.invalid')
    return folder


def seed(tmp_path, repository, value, *, bad_digest=False):
    (repository/'source.txt').write_text(value)
    git(repository,'add','source.txt');git(repository,'commit','-qm',value)
    commit=git(repository,'rev-parse','HEAD')
    bundle=tmp_path/(commit+'.bundle')
    git(repository,'bundle','create',str(bundle),'HEAD')
    content=bundle.read_bytes()
    digest='0'*64 if bad_digest else hashlib.sha256(content).hexdigest()
    config=tmp_path/'node/peers.json';config.parent.mkdir(exist_ok=True)
    config.write_text('{}')
    result=subprocess.run([sys.executable,'-c',install_code(commit,digest)],input=content,
                          capture_output=True,env={**os.environ,'LIFETECH_PEER_CONFIG':str(config)})
    return commit,result,config.parent/'node-source/HarborGenetics.git'


def test_local_mirror_fetches_pinned_source_and_preserves_earlier_commit(tmp_path, repository):
    first,result,cache=seed(tmp_path,repository,'first')
    assert result.returncode==0,result.stderr.decode()
    assert git(cache,'show',first+':source.txt')=='first'
    second,result,cache=seed(tmp_path,repository,'second')
    assert result.returncode==0,result.stderr.decode()
    assert git(cache,'show',second+':source.txt')=='second'
    assert git(cache,'show',first+':source.txt')=='first'
    for bundle in (cache.parent/'mirrors').glob('*.bundle'):
        assert bundle.stat().st_mode & 0o777 == 0o600


def test_bad_transfer_checksum_cannot_create_source_cache(tmp_path, repository):
    _,result,cache=seed(tmp_path,repository,'rejected',bad_digest=True)
    assert result.returncode!=0
    assert b'checksum differs' in result.stderr
    assert not cache.exists()
    assert not list((cache.parent/'mirrors').glob('*.bundle'))
