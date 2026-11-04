import pytest
from wgs_eta import estimate_eta


def points(*pairs):
    return [{'timestamp':t,'completed':c} for t,c in pairs]


def test_sustained_rate_and_complete():
    value=estimate_eta(points((0,0),(60,120),(120,240)),1000,now=120)
    assert value['status']=='estimating'
    assert value['rate_per_second']==2 and value['seconds_remaining']==380
    assert value['optimistic_seconds']==value['pessimistic_seconds']==380
    assert estimate_eta(points((120,1000)),1000,now=120)['status']=='complete'


def test_reset_and_generation_change_do_not_reuse_old_rate():
    assert estimate_eta(points((0,500),(60,800),(90,10)),1000,now=90)['status']=='insufficient_data'
    rows=[{'timestamp':0,'completed':0,'generation':'a'}, {'timestamp':60,'completed':800,'generation':'b'}]
    assert estimate_eta(rows,1000,now=60)['status']=='insufficient_data'


def test_blocked_stale_and_stalled_have_no_finite_eta():
    rows=points((0,0),(60,100),(240,100))
    for kwargs,status in [({'now':240,'blocked':True},'blocked'),({'now':400},'stale'),({'now':240},'stalled')]:
        value=estimate_eta(rows,1000,**kwargs)
        assert value['status']==status and value['seconds_remaining'] is None


def test_chunk_bursts_include_idle_time_and_variable_bounds():
    value=estimate_eta(points((0,0),(60,100),(120,100),(180,300)),1000,now=180)
    assert value['rate_per_second']==pytest.approx(300/180)
    assert value['optimistic_seconds']==210
    assert value['pessimistic_seconds'] is None  # observed idle window


def test_invalid_duplicates_and_out_of_order_samples():
    rows=points((0,0),(60,100),(30,50),(60,120),(90,float('nan')),(1000,999))
    value=estimate_eta(rows,1000,now=60)
    assert value['rate_per_second']==2
    with pytest.raises(ValueError):estimate_eta([],float('nan'),now=0)
    with pytest.raises(ValueError):estimate_eta([],100,now=0,min_span=0)


def test_live_tracker_parallel_sources_block_missing_counters_and_restarts():
    from wgs_eta import TransferEstimator
    tracker=TransferEstimator()
    def row(t,n,status='downloading'):
        return {'bytes':1000,'status':status,'progress':{'updated':t,'bytes_completed':n}}
    assert tracker.sample('sample',[('a',row(0,0)),('b',row(0,0))],now=0)['status']=='insufficient_data'
    value=tracker.sample('sample',[('a',row(60,120)),('b',row(60,60))],now=60)
    assert value['seconds_remaining']==940  # slower parallel receiver
    assert tracker.sample('sample',[('a',row(65,120)),('b',row(65,60,'failed'))],now=65)['status']=='blocked'
    assert tracker.sample('sample',[('a',row(70,130))],now=70,generation='new')['status']=='insufficient_data'
    assert tracker.sample('other',[('b',{'bytes':1000,'status':'downloading'})],now=70)['status']=='insufficient_data'
    assert tracker.sample('complete',[('a',{'bytes':1000,'status':'verified'})],now=70)['status']=='complete'


def test_cache_rehash_is_never_reported_as_network_speed():
    from wgs_eta import TransferEstimator
    tracker=TransferEstimator()
    def source(t,n,state):return [('x',{'bytes':1000,'status':state,'progress':{'updated':t,'bytes_completed':n}})]
    assert tracker.sample('s',source(0,0,'verifying_cached_chunks'),now=0)['status']=='verifying_cache'
    assert tracker.sample('s',source(60,800,'verifying_cached_chunks'),now=60)['seconds_remaining'] is None
    assert tracker.sample('s',source(70,800,'downloading'),now=70)['status']=='insufficient_data'
