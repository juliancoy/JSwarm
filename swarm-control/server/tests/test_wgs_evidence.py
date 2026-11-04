import hashlib
from wgs_evidence import verified_sources


def fixture():
    item={'url':'https://archive.example/public.gz','filename':'public.gz','bytes':100,'md5':'a'*32,'read_count':20,'sample':'Watson','run':'public'}
    ident=hashlib.sha256(item['url'].encode()).hexdigest()[:20]
    qc={'phase':'verified','provisional':False,'sample':'Watson','run':'public','filename':'public.gz','input_md5':item['md5'],'reads':20,'expected_reads':20,'compressed_bytes_read':100,'compressed_bytes_total':100}
    return item,ident,qc


def test_complete_manifest_bound_qc_survives_worker_readiness_failure():
    item,ident,qc=fixture()
    downloads={ident:{'status':'failed','error':'Worker identity/policy changed'}}
    assert verified_sources([item],downloads,{ident:qc})=={ident:'verified_full_source_qc'}
    assert verified_sources([item],{ident:{'status':'verified','receipt':item}},{} )=={ident:'publisher_receipt'}


def test_partial_provisional_wrong_source_and_mismatched_counts_never_unlock_finalization():
    item,ident,qc=fixture()
    for change in [{'phase':'streaming'},{'provisional':True},{'input_md5':'b'*32},{'sample':'Venter'},{'run':'other'},{'filename':'other.gz'},{'reads':19},{'expected_reads':19},{'compressed_bytes_read':99},{'compressed_bytes_total':99}]:
        assert not verified_sources([item],{ident:{'status':'failed'}},{ident:{**qc,**change}})
    assert not verified_sources([item],{ident:{'status':'verified','receipt':{**item,'md5':'b'*32}}},{})
    assert not verified_sources([item],{ident:{'status':'verified'}},{})


def test_fresh_full_file_recheck_can_reconcile_legacy_qc_missing_byte_counters():
    item,ident,qc=fixture();qc.pop('compressed_bytes_read');qc.pop('compressed_bytes_total')
    receipt={**item,'verification':'publisher_md5_rechecked','sha256':'c'*64,'verified_at':100}
    assert verified_sources([item],{}, {ident:qc},{ident:receipt})=={ident:'publisher_md5_rechecked'}
    for change in [{'md5':'b'*32},{'bytes':99},{'url':'https://other.example/public.gz'},{'verification':'unverified'},{'sha256':'bad'},{'verified_at':0}]:
        assert not verified_sources([item],{}, {ident:qc},{ident:{**receipt,**change}})
