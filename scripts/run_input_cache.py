"""Private bounded block cache shared by run-manager processes on one host.

The OS page cache supplies the RAM tier. Per-block flock coalesces simultaneous
fetches. A full publisher digest is still required by the consuming executor.
"""
import fcntl
import hashlib
import os
from pathlib import Path
import re
import shutil
import tempfile

BLOCK = 4 * 1024**2

def ram_cache(budget=512*1024**2):
    """Bounded shared tmpfs tier; never silently fall back to a disk cache."""
    root=Path('/dev/shm')/('swarm-inputs-'+str(os.getuid()))
    root.mkdir(mode=0o700,exist_ok=True)
    if root.is_symlink() or root.stat().st_uid!=os.getuid():raise ValueError('Unsafe RAM cache owner')
    root.chmod(0o700)
    memory={line.split(':')[0]:int(line.split()[1])*1024 for line in Path('/proc/meminfo').read_text().splitlines() if line.split()[1].isdigit()}
    return InputCache(root,budget=max(BLOCK,min(budget,memory['MemAvailable']//8)),reserve=128*1024**2)

class InputCache:
    def __init__(self, root, budget=2*1024**3, reserve=1024**3):
        self.root=Path(root); self.root.mkdir(parents=True,exist_ok=True,mode=0o700)
        self.budget=budget; self.reserve=reserve

    def lookup(self,identity,total,offset):
        if not re.fullmatch('[0-9a-f]{32}',identity) or not 0<=offset<total or offset%BLOCK:
            raise ValueError('Invalid cache object/range')
        length=min(BLOCK,total-offset)
        key=hashlib.sha256(f'{identity}:{total}:{offset}:{length}'.encode()).hexdigest()
        path=self.root/(key+'.block')
        try:payload=path.read_bytes()
        except FileNotFoundError:return None
        if len(payload)!=length+32 or hashlib.sha256(payload[32:]).digest()!=payload[:32]:return None
        try:os.utime(path,None)
        except FileNotFoundError:pass
        return payload[32:]

    def block(self, identity, total, offset, fetch):
        if not re.fullmatch('[0-9a-f]{32}',identity) or not 0<=offset<total or offset%BLOCK:
            raise ValueError('Invalid cache object/range')
        length=min(BLOCK,total-offset)
        key=hashlib.sha256(f'{identity}:{total}:{offset}:{length}'.encode()).hexdigest()
        # Fixed lock stripes avoid unbounded lock-file growth and lock-inode races.
        with (self.root/('lock-'+key[:2])).open('a') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX)
            path=self.root/(key+'.block')
            try:payload=path.read_bytes()
            except FileNotFoundError:payload=b''
            if len(payload)==length+32 and hashlib.sha256(payload[32:]).digest()==payload[:32]:
                try:os.utime(path,None)
                except FileNotFoundError:pass
                return payload[32:]
            path.unlink(missing_ok=True)
            data=fetch(offset,length)
            if len(data)!=length:raise ValueError('Incomplete input range')
            # Serialize eviction/admission; returned bytes remain valid after eviction.
            with (self.root/'eviction.lock').open('a') as eviction:
                fcntl.flock(eviction,fcntl.LOCK_EX)
                files=sorted(self.root.glob('*.block'),key=lambda p:p.stat().st_mtime)
                used=sum(p.stat().st_size for p in files)
                required=len(data)+32
                while files and (used+required>self.budget or shutil.disk_usage(self.root).free<self.reserve+required):
                    victim=files.pop(0);size=victim.stat().st_size;victim.unlink();used-=size
                if required<=self.budget and shutil.disk_usage(self.root).free>=self.reserve+required:
                    descriptor,temp=tempfile.mkstemp(prefix='partial-',dir=self.root)
                    try:
                        with os.fdopen(descriptor,'wb') as output:
                            output.write(hashlib.sha256(data).digest());output.write(data)
                        os.replace(temp,path)
                    finally:
                        Path(temp).unlink(missing_ok=True)
            return data

class CachedInput:
    """Sequential bounded reader backed by authenticated peer byte ranges."""
    def __init__(self,cache,identity,total,fetch):
        self.cache=cache;self.identity=identity;self.total=total;self.fetch=fetch
        self.offset=0;self.pending=b'';self.position=0
    def readinto(self,buffer):
        if self.position==len(self.pending):
            if self.offset==self.total:return 0
            self.pending=self.cache.block(self.identity,self.total,self.offset,self.fetch)
            self.offset+=len(self.pending);self.position=0
        n=min(len(buffer),len(self.pending)-self.position)
        buffer[:n]=memoryview(self.pending)[self.position:self.position+n];self.position+=n
        return n
