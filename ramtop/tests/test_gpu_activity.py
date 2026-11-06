from ramtop.gpu import device_activity


def test_native_drm_devices_merge_engines_without_inventing_idle(tmp_path):
    for card,pci,vendor in [('card0','0000:00:02.0','0x8086'),('card1','0000:04:00.0','0x1002')]:
        device=tmp_path/pci;device.mkdir();(device/'vendor').write_text(vendor)
        (tmp_path/card).mkdir();(tmp_path/card/'device').symlink_to(device)
    (tmp_path/'0000:04:00.0/gpu_busy_percent').write_text('0')
    rows=device_activity({'clients':[{'device':'0000:00:02.0','engines':{'render':20,'copy':3}},{'device':'0000:00:02.0','engines':{'render':30}}]},tmp_path,query=lambda:'')
    assert rows['0000:00:02.0']['utilization_percent']==50
    assert rows['0000:04:00.0']['utilization_percent']==0
    missing=device_activity({},tmp_path,query=lambda:'')['0000:00:02.0']
    assert 'utilization_percent' not in missing and 'required' in missing['utilization_unavailable_reason']


def test_native_nvidia_runtime_counters_keep_device_identity_and_unknowns(tmp_path):
    rows=device_activity({},tmp_path,query=lambda:'00000000:04:00.0, GPU-id, RTX, 63, 12288, 4096\n00000000:01:00.0, GPU-other, MX, N/A, 2048, 1024')
    assert rows['0000:04:00.0']['utilization_percent']==63
    assert rows['0000:04:00.0']['memory_free_mib']==4096
    assert 'utilization_percent' not in rows['0000:01:00.0']


def test_unresponsive_nvidia_query_never_blocks_or_spawns_overlapping_queries():
    import threading,time
    from ramtop.gpu import NvidiaQuery
    entered=threading.Event();release=threading.Event();calls=[]
    def query():calls.append(True);entered.set();release.wait(2);return 'measured'
    sampler=NvidiaQuery(query)
    try:
        before=time.monotonic();assert sampler()==''
        assert time.monotonic()-before<.5 and entered.wait(1)
        for _ in range(5):assert sampler()==''
        assert len(calls)==1
    finally:release.set();sampler.thread.join(timeout=1)
    assert sampler()=='measured'
