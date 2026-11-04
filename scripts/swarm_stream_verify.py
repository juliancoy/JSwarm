"""Bounded-memory verification of compressed objects streamed from an enrolled peer."""
import gzip
import hashlib
import json
from pathlib import Path
import subprocess
import time


def verify(stream, expected_sha256, compressed_size, raw_size, progress=None):
    class Reader:
        count=0
        digest=hashlib.sha256()
        def read(self, size=-1):
            block=stream.read(min(size,1024**2) if size>=0 else 1024**2)
            self.count+=len(block)
            if self.count>compressed_size:raise ValueError('Compressed stream exceeded receipt size')
            self.digest.update(block)
            return block
    reader=Reader();raw=hashlib.sha256();count=0;last=0
    with gzip.GzipFile(fileobj=reader,mode='rb') as data:
        while block:=data.read(1024**2):
            raw.update(block);count+=len(block)
            if count>raw_size:raise ValueError('Decompressed stream exceeded canonical size')
            if progress and time.monotonic()-last>=2:
                progress({'phase':'streaming decompressed-content verification','compressed_bytes':reader.count,'completed_bytes':count,'total_bytes':raw_size,'updated':time.time()});last=time.monotonic()
    if reader.count!=compressed_size or reader.digest.hexdigest()!=expected_sha256:
        raise ValueError('Compressed stream checksum or size mismatch')
    if count!=raw_size:raise ValueError('Decompressed content length mismatch')
    return {'compressed_sha256':reader.digest.hexdigest(),'decompressed_sha256':raw.hexdigest(),'decompressed_bytes':count,'compressed_bytes':reader.count,'verified_at':time.time()}


def stream_error(process, error):
    """A truncated network stream must retry; a valid transport with bad data stops."""
    try:code=process.wait(timeout=2)
    except subprocess.TimeoutExpired:return error
    if code not in (0,22,51,58,60,90):return ConnectionError('Stream transport interrupted; content receipt not published')
    return error


def main():
    import argparse,os,re,sys
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ('source','machine','object','sha256','output','progress'):parser.add_argument('--'+name,required=True)
    parser.add_argument('--size',type=int,required=True);parser.add_argument('--raw-size',type=int,required=True)
    args=parser.parse_args();os.umask(0o077)
    from bulk import load_config,BASE
    from transfer import atomic_json
    config=load_config();peer=config['peers'].get(args.source)
    if not peer or peer['machine']!=args.machine or not re.fullmatch('[0-9a-f]{64}',args.object):raise ValueError('Unknown source identity or object')
    if not re.fullmatch('[0-9a-f]{64}',args.sha256) or min(args.size,args.raw_size)<1:raise ValueError('Invalid immutable receipt')
    command=['curl','--silent','--show-error','--fail','--noproxy','*','--proto','=https','--tlsv1.3',
             '--cacert',str(BASE/'ca.crt'),'--cert',str(BASE/'identity.crt'),'--key',str(BASE/'identity.key'),
             '--pinnedpubkey',peer['public_key_pin'],'--header','If-Match: "'+args.sha256+'"',
             '--connect-timeout','10','--speed-time','60','--speed-limit','1024',
             'https://'+peer['address']+':'+str(config['port'])+'/objects/'+args.object]
    process=subprocess.Popen(command,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
    try:
        try:
            result=verify(process.stdout,args.sha256,args.size,args.raw_size,lambda row:atomic_json(args.progress,row))
        except (ValueError,EOFError,gzip.BadGzipFile) as error:
            raise stream_error(process,error) from error
        process.wait(timeout=20)
        if process.returncode:raise ConnectionError('Verified stream transport failed')
        atomic_json(args.output,{**result,'worker':config['name'],'worker_machine':config['machine'],'source':args.source,'source_machine':args.machine})
    finally:
        if process.poll() is None:process.terminate();process.wait(timeout=20)
        process.stdout.close();process.stderr.close()


if __name__=='__main__':
    try:main()
    except (ValueError,EOFError,gzip.BadGzipFile) as error:raise SystemExit(78) from error
