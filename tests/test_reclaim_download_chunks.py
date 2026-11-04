import hashlib
import json
from scripts.reclaim_download_chunks import reclaim


def test_only_ranges_matching_retained_archive_are_reclaimed(tmp_path):
    content=b'original immutable bytes';(tmp_path/'archive.gz').write_bytes(content)
    (tmp_path/'receipt.json').write_text(json.dumps({'filename':'archive.gz','bytes':len(content)}))
    chunks=tmp_path/'chunks';chunks.mkdir()
    for n,data in [(0,content[:8]),(1,b'corrupt')]:
        (chunks/(str(n)+'.bin')).write_bytes(data)
        (chunks/(str(n)+'.json')).write_text(json.dumps({'start':0 if n==0 else 8,'bytes':len(data),'sha256':hashlib.sha256(data).hexdigest()}))
    reclaim(tmp_path)
    assert (tmp_path/'archive.gz').read_bytes()==content
    assert not (chunks/'0.bin').exists()
    assert (chunks/'1.bin').exists()
