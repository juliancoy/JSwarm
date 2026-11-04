import gzip
import hashlib
import io
import pytest
from scripts.swarm_stream_verify import verify


def fixture():
    raw=b'@synthetic\nACGT\n+\nIIII\n'*10000
    zipped=gzip.compress(raw,mtime=0)
    return raw,zipped,hashlib.sha256(zipped).hexdigest()


def test_stream_verifies_compressed_and_decompressed_content():
    raw,zipped,sha=fixture();progress=[]
    result=verify(io.BytesIO(zipped),sha,len(zipped),len(raw),progress.append)
    assert result['decompressed_sha256']==hashlib.sha256(raw).hexdigest()
    assert result['compressed_sha256']==sha
    assert progress


@pytest.mark.parametrize('mistake',['digest','compressed_size','raw_size','crc','truncated'])
def test_bad_stream_cannot_produce_verified_receipt(mistake):
    raw,zipped,sha=fixture();size=len(zipped);rawsize=len(raw)
    if mistake=='digest':sha='0'*64
    elif mistake=='compressed_size':size-=1
    elif mistake=='raw_size':rawsize-=1
    elif mistake=='crc':zipped=zipped[:-8]+bytes([zipped[-8]^1])+zipped[-7:]
    else:zipped=zipped[:-5]
    with pytest.raises((ValueError,EOFError,gzip.BadGzipFile)):
        verify(io.BytesIO(zipped),sha,size,rawsize)


@pytest.mark.parametrize('code,connection_error',[(18,True),(7,True),(28,True),(0,False),(22,False),(60,False)])
def test_network_truncation_retries_but_integrity_and_identity_rejection_stop(code,connection_error):
    import subprocess,sys
    from scripts.swarm_stream_verify import stream_error
    process=subprocess.Popen([sys.executable,'-c','import sys;sys.exit('+str(code)+')'])
    error=EOFError('Truncated gzip stream')
    result=stream_error(process,error)
    assert isinstance(result,ConnectionError) if connection_error else result is error
