"""Actual kernel mmap through both supported system FUSE ABIs, no VM."""
import json
import mmap
import os
from pathlib import Path
import subprocess
import sys
import time
import pytest

CHILD=r'''
import json,sys
from pathlib import Path
sys.path.insert(0,sys.argv[1]);sys.path.insert(0,sys.argv[2])
from scripts import swarm_fs_mount as adapter
from scripts.swarm_filesystem import Filesystem,request
settings=json.loads(Path(sys.argv[3]).read_text());service=Filesystem(Path(sys.argv[3]))
def call(self,node,operation,**args):
 args.pop('timeout',None)
 result=service.dispatch(request(settings,operation,**args))
 if 'errno' in result:raise OSError(result['errno'],'filesystem request failed')
 return result['result']
adapter.Client.call=call
adapter.mount(settings,Path(sys.argv[4]),True)
'''


@pytest.mark.parametrize('major',['2','3'])
def test_real_kernel_mapping_and_normal_direct_io(tmp_path,major):
    import ctypes.util
    if not Path('/dev/fuse').exists() or not ctypes.util.find_library('fuse3' if major=='3' else 'fuse'):
        pytest.skip('Host FUSE device/library unavailable')
    dependencies=Path.home()/'.local/share/swarm-filesystem/dependencies'
    if not (dependencies/'mfusepy.py').exists():pytest.skip('Host mfusepy installation unavailable')
    from scripts.swarm_filesystem import version
    source=tmp_path/'origin';source.mkdir();data=bytes(range(256))*256
    reference=source/'reference';reference.write_bytes(data);reference.chmod(0o444)
    (source/'ordinary').write_bytes(b'old')
    settings={'namespace':'mmap-fixture','origin':'fixture.local','secret':'ab'*32,'root':str(source),
              'peers':[],'minimum_free_bytes':0,'fuse_version':major,'immutable_files':{'/reference':version(reference.stat())}}
    config=tmp_path/'filesystem.json';config.write_text(json.dumps(settings));mountpoint=tmp_path/'mnt';mountpoint.mkdir()
    project=Path(__file__).resolve().parents[1]
    with (tmp_path/'stderr').open('w+') as errors:
        process=subprocess.Popen([sys.executable,'-c',CHILD,str(dependencies),str(project),str(config),str(mountpoint)],stderr=errors,stdout=errors)
        try:
            deadline=time.monotonic()+15
            while not os.path.ismount(mountpoint) and time.monotonic()<deadline and process.poll() is None:time.sleep(.05)
            errors.flush();errors.seek(0)
            assert os.path.ismount(mountpoint),errors.read()
            with (mountpoint/'reference').open('rb') as stream:
                with mmap.mmap(stream.fileno(),0,access=mmap.ACCESS_READ) as pages:
                    assert pages[4096:8192]==data[4096:8192]
            with pytest.raises(OSError):(mountpoint/'reference').open('wb')
            with (mountpoint/'ordinary').open('r+b',buffering=0) as stream:stream.write(b'new')
            assert (source/'ordinary').read_bytes()==b'new'
        finally:
            if os.path.ismount(mountpoint):subprocess.run(['fusermount3' if major=='3' else 'fusermount','-u',str(mountpoint)],capture_output=True,timeout=10)
            if process.poll() is None:process.terminate()
            process.wait(timeout=10)
