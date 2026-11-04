import hashlib
import io
import threading
from concurrent.futures import ThreadPoolExecutor
import pytest
from scripts.run_input_cache import InputCache,CachedInput,BLOCK
from scripts.venter_stream_offload import pump


def test_concurrent_consumers_fetch_once_and_next_run_reuses(tmp_path):
    raw=b'dna'*1000;identity=hashlib.md5(raw).hexdigest();calls=[]
    gate=threading.Barrier(4)
    def consume(_):
        gate.wait(timeout=3)
        cache=InputCache(tmp_path,reserve=0)
        def fetch(offset,length):calls.append(offset);return raw[offset:offset+length]
        out=io.BytesIO();pump(CachedInput(cache,identity,len(raw),fetch),out,len(raw),identity)
        return out.getvalue()
    with ThreadPoolExecutor(4) as pool:assert list(pool.map(consume,range(4)))==[raw]*4
    assert calls==[0]
    assert InputCache(tmp_path,reserve=0).block(identity,len(raw),0,lambda *_:pytest.fail('refetched'))==raw


def test_corrupt_cache_refetched_and_wrong_source_fails_full_digest(tmp_path):
    raw=b'original';identity=hashlib.md5(raw).hexdigest();cache=InputCache(tmp_path,reserve=0)
    cache.block(identity,len(raw),0,lambda *_:raw)
    next(tmp_path.glob('*.block')).write_bytes(b'corrupt')
    assert cache.block(identity,len(raw),0,lambda *_:raw)==raw
    other=InputCache(tmp_path/'other',reserve=0)
    with pytest.raises(ValueError,match='MD5'):
        pump(CachedInput(other,identity,len(raw),lambda *_:b'incorrect'[:len(raw)]),io.BytesIO(),len(raw),identity)


def test_lru_budget_eviction_and_no_space_bypass(tmp_path):
    cache=InputCache(tmp_path,budget=100,reserve=0)
    for value in (b'a'*40,b'b'*40):
        cache.block(hashlib.md5(value).hexdigest(),len(value),0,lambda *_:value)
    assert sum(p.stat().st_size for p in tmp_path.glob('*.block'))<=100
    cache=InputCache(tmp_path/'no-space',budget=1,reserve=0)
    assert cache.block('a'*32,4,0,lambda *_:b'test')==b'test'
    assert not list(cache.root.glob('*.block'))


def test_multiblock_stream_and_short_range(tmp_path):
    raw=b'x'*(BLOCK+42);cache=InputCache(tmp_path,reserve=0);seen=[]
    def fetch(offset,length):seen.append((offset,length));return raw[offset:offset+length]
    out=io.BytesIO();identity=hashlib.md5(raw).hexdigest()
    pump(CachedInput(cache,identity,len(raw),fetch),out,len(raw),identity)
    assert out.getvalue()==raw and seen==[(0,BLOCK),(BLOCK,42)]
    with pytest.raises(ValueError,match='Incomplete'):
        cache.block('b'*32,10,0,lambda *_:b'short')


def test_source_range_authorization():
    from scripts.run_input_source import byte_range
    assert byte_range(None,100)==(0,99)
    assert byte_range('bytes=10-19',100)==(10,19)
    with pytest.raises(ValueError):byte_range('bytes=0-100',100)
    for requested in ['bytes=-1-9','bytes=10-9','bytes=0-9,20-29','bytes=0-4194304']:
        with pytest.raises(ValueError):byte_range(requested,10*BLOCK)
