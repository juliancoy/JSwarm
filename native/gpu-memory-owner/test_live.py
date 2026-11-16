"""Small, admitted hardware test. No genomic reference or existing service edits."""
import array
import concurrent.futures
import hashlib
import fcntl
import mmap
import struct
import json
import os
from pathlib import Path
import subprocess
import socket
import sys
import tempfile
import time
import uuid

ROOT=Path(__file__).resolve().parents[3]
sys.path[:0]=[str(ROOT/'JSwarm'),str(ROOT/'JSwarm/scripts'),str(ROOT/'JSwarm/swarm-control/server')]
from swarm_gpu_memory import request,header,Session,MAX_QUERIES,receive
from agent_client import AgentClient,CONFIG
from scripts.launcher_discovery import resolve_endpoint


def client_task(values):
    path,digest,indices=values
    return request(path,digest,indices)


def main():
    config=json.loads(CONFIG.read_text());name=config['name'];endpoint,_=resolve_endpoint(name)
    agent=AgentClient({'host':name,'agent_endpoint':endpoint,'local':True})
    lease=uuid.uuid4().hex
    try:
        admission=agent.call('resource.acquire',{'id':lease,'group':'gpu-memory-owner-test','cpus':1,'memory_mb':512,'container':'swarm-job-'+lease,'sink':'gpu','priority':-100},timeout=15)
        if not admission.get('acquired'):raise RuntimeError('No capacity for bounded hardware test')
        with tempfile.TemporaryDirectory(prefix='gmo-',dir='/tmp') as temp:
            folder=Path(temp); source=folder/'object';ready=folder/'ready'
            words=array.array('I',((i*2654435761^0x5a5aa5a5)&0xffffffff for i in range(2*1024*1024)))
            source.write_bytes(words.tobytes()); source.chmod(0o400)
            digest=hashlib.sha256(source.read_bytes()).hexdigest()
            command=[sys.executable,str(ROOT/'JSwarm/scripts/swarm_gpu_memory.py'),'--source',str(source),'--sha256',digest,'--directory',str(folder/'owner'),'--ready',str(ready),'--budget',str(8*1024**2),'--segment',str(2*1024**2),'--max-sessions','4']
            command.extend(sys.argv[1:])
            with (folder/'stderr').open('w+') as err:
                process=subprocess.Popen(command,stderr=err)
                try:
                    deadline=time.monotonic()+20
                    while not ready.exists() and process.poll() is None and time.monotonic()<deadline:time.sleep(.05)
                    if not ready.exists():
                        err.seek(0);raise RuntimeError(err.read())
                    info=json.loads(ready.read_text()); path=info['socket']
                    indices=[0,1,524287,524288,1048575,1048576,1572863,1572864,len(words)-1]+[(i*7919)%len(words) for i in range(1000)]
                    segment=int([command[i+1] for i,value in enumerate(command) if value=='--segment'][-1])//4
                    indices.extend(i for i in (segment-1,segment,2*segment-1,2*segment) if i<len(words))
                    batches=[[((i+batch*131)%len(words)) for i in indices] for batch in range(6)]
                    with concurrent.futures.ProcessPoolExecutor(max_workers=3) as pool:
                        results=list(pool.map(client_task,[(path,digest,batch) for batch in batches]))
                    assert all(result==[words[i] for i in batch] for result,batch in zip(results,batches))
                    after=request(path,digest)
                    assert after['reference_upload_bytes']==source.stat().st_size
                    assert after['reference_allocation_bytes']==info['reference_allocation_bytes']
                    assert after['segments']==info['segments'] and after['query_batches']==6
                    assert after['query_upload_copy_bytes']==after['result_readback_copy_bytes']
                    assert (after['query_upload_copy_bytes']==0)==(after['device_io']=='mapped')
                    assert after['parallelism']==4
                    assert after['socket_payload_bytes']==0
                    # Direct shared views: no payload packing, socket send, or list
                    # result creation is required by the Session API.
                    with Session(path,digest) as direct:
                        assert direct.metadata['device_io']==after['device_io']
                        assert direct.metadata['gpu_payload_zero_copy']==(after['device_io']=='mapped')
                        assert fcntl.fcntl(direct._fd,fcntl.F_GETFD)&fcntl.FD_CLOEXEC
                        try:os.ftruncate(direct._fd,0)
                        except PermissionError:pass
                        else:raise AssertionError('shared mapping can be truncated')
                        recordings_before=request(path,digest)['command_recordings']
                        for iteration in range(5):
                            for i in range(MAX_QUERIES):direct.inputs[i]=(i*7919+iteration*131)%len(words)
                            direct.gather(MAX_QUERIES)
                            assert all(direct.outputs[i]==words[direct.inputs[i]] for i in range(MAX_QUERIES))
                        recordings_after=request(path,digest)['command_recordings']
                        assert recordings_after-recordings_before==(1 if after['command_reuse_enabled'] else 5)
                        for size in (1,63,64,65,4095,4096,4097,MAX_QUERIES-1,MAX_QUERIES):
                            direct.gather(size)
                            assert all(direct.outputs[i]==words[direct.inputs[i]] for i in range(size))
                        # Binary control handles partial frames and rejects bad
                        # counts/bounds without poisoning a healthy session.
                        direct.inputs[0]=123
                        frame=struct.pack('<I',1)
                        direct.socket.sendall(frame[:1]);time.sleep(.01)
                        direct.socket.sendall(frame[1:])
                        status=bytearray(4);receive(direct.socket,memoryview(status))
                        assert struct.unpack('<I',status)==(0,) and direct.outputs[0]==words[123]
                        direct.socket.sendall(struct.pack('<I',MAX_QUERIES+1))
                        receive(direct.socket,memoryview(status))
                        assert struct.unpack('<I',status)==(1,)
                        direct.inputs[0]=0xffffffff
                        try:direct.gather(1)
                        except RuntimeError:pass
                        else:raise AssertionError('native invalid index accepted')
                        direct.inputs[0]=123;direct.gather(1)
                        assert direct.outputs[0]==words[123]
                        stale_fd=os.dup(direct._fd)
                        old_inode=os.fstat(stale_fd).st_ino
                        stale=mmap.mmap(stale_fd,direct.metadata['mapping_bytes'])
                    # Force reuse of all four server slots; retained old mappings
                    # must never alias a subsequent session's mapped request pages.
                    try:
                        for iteration in range(4):
                            with Session(path,digest) as fresh:
                                assert os.fstat(fresh._fd).st_ino!=old_inode
                                fresh.inputs[0]=iteration+123
                                stale[:4]=struct.pack('<I',0xffffffff)
                                fresh.gather(1)
                                assert fresh.outputs[0]==words[iteration+123]
                    finally:stale.close();os.close(stale_fd)
                    assert request(path,digest)['socket_payload_bytes']==0
                    # Explicit socket fallback uses the same persistent protocol.
                    with Session(path,digest,'socket') as fallback:
                        for i in range(64):fallback.inputs[i]=i*13
                        fallback.gather(64)
                        assert all(fallback.outputs[i]==words[i*13] for i in range(64))
                    # Counters update after sendall; a metrics reader can run
                    # before the sender reacquires the GIL to publish that update.
                    for _ in range(100):
                        wire_bytes=request(path,digest)['socket_payload_bytes']
                        if wire_bytes==64*8:break
                        time.sleep(.01)
                    assert wire_bytes==64*8
                    # Hold one slot waiting for a payload; another must progress.
                    with socket.socket(socket.AF_UNIX) as stalled:
                        stalled.connect(path)
                        header(stalled,{'sha256':digest,'count':1})
                        assert header(stalled)['accepted']
                        started=time.monotonic()
                        assert request(path,digest,[123])==[words[123]]
                        assert time.monotonic()-started<3
                    for bad_digest,bad_indices in [('0'*64,[0]),(digest,[len(words)])]:
                        try:request(path,bad_digest,bad_indices)
                        except RuntimeError:pass
                        else:raise AssertionError('invalid request accepted')
                    # A disconnected client must not terminate the owner.
                    with socket.socket(socket.AF_UNIX) as abandoned:
                        abandoned.connect(path)
                        abandoned.sendall(b'\x00\x00\x00\x01{')
                    assert request(path,digest,[1])==[words[1]]
                    duplicate=subprocess.run(command,capture_output=True,text=True,timeout=15)
                    assert duplicate.returncode and 'existing owner' in duplicate.stderr
                    assert request(path,digest)['reference_upload_bytes']==source.stat().st_size
                    print(json.dumps({'result':'PASS','clients':3,'batches':6,'queries_per_batch':len(indices),'owner':after}))
                finally:
                    process.terminate()
                    try:process.wait(timeout=5)
                    except subprocess.TimeoutExpired:process.kill();process.wait()
    finally:
        try:agent.call('resource.release',{'id':lease},timeout=15)
        finally:agent.close()

if __name__=='__main__':main()
