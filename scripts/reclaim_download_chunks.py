"""Reclaim only byte-verified duplicate ranges of a retained complete archive."""
import fcntl
import hashlib
import json
import os
from pathlib import Path
from scripts.launcher_streams import atomic_json


def reclaim(folder):
    folder=Path(folder);receipt=json.loads((folder/'receipt.json').read_text())
    archive=folder/receipt['filename']
    if archive.is_symlink() or archive.stat().st_size!=receipt['bytes']:raise ValueError('Complete retained archive required')
    removed=[];freed=0
    with (folder/'download.lock').open('a') as lock,archive.open('rb') as full:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        identity=lambda p:(p.stat().st_dev,p.stat().st_ino,p.stat().st_size,p.stat().st_mtime_ns,p.stat().st_ctime_ns)
        before=identity(archive)
        for metadata in sorted((folder/'chunks').glob('*.json'),key=lambda p:int(p.stem)):
            chunk=metadata.with_suffix('.bin')
            if not chunk.exists():continue
            if chunk.is_symlink():raise ValueError('Unexpected chunk symlink')
            spec=json.loads(metadata.read_text());initial=identity(chunk)
            if chunk.stat().st_size!=spec['bytes']:continue
            full.seek(spec['start']);left=spec['bytes'];one=hashlib.sha256();two=hashlib.sha256()
            with chunk.open('rb') as source:
                while left:
                    amount=min(left,1024*1024);a=source.read(amount);b=full.read(amount)
                    if len(a)!=amount or len(b)!=amount:raise ValueError('Range changed during verification')
                    one.update(a);two.update(b);left-=amount
            if one.hexdigest()!=spec['sha256'] or two.hexdigest()!=spec['sha256']:continue
            if identity(archive)!=before or identity(chunk)!=initial:raise ValueError('Input changed during verification')
            allocated=chunk.stat().st_blocks*512
            chunk.unlink();freed+=allocated;removed.append({'chunk':chunk.name,'bytes':spec['bytes'],'sha256':spec['sha256']})
            atomic_json(folder/'duplicate-reclamation.json',{'archive':str(archive),'freed_bytes':freed,'removed':removed,'basis':'Chunk and retained archive range independently match committed SHA256'})
            print(json.dumps({'chunk':chunk.name,'freed_gib':round(freed/1024**3,2)}),flush=True)
    return freed


if __name__=='__main__':
    import sys
    reclaim(sys.argv[1])
