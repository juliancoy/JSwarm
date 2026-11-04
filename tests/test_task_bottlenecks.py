from scripts.task_bottlenecks import classify


def row(**kwargs):return {'sampled_at':100,'running':True,'reserved_cpus':1,'cpu_percent':99,**kwargs}


def test_cpu_saturation_uses_reserved_cores():
    assert classify(row(),100)['label']=='CPU allocation saturated'
    assert classify(row(reserved_cpus=8),100)['label']=='Low CPU parallelism in current stage'


def test_storage_is_only_inferred_from_combined_host_pressure():
    result=classify(row(host_metrics={'cpu_iowait_percent':25,'disk_busy_percent':95}),100)
    assert result['label']=='Possible storage contention' and result['confidence']=='inferred'


def test_network_traffic_alone_does_not_establish_bottleneck():
    result=classify(row(cpu_percent=5,host_metrics={'network_mbps':900,'cpu_iowait_percent':None}),100)
    assert result['label']=='No dominant resource established'
    assert 'all workloads' in result['evidence']


def test_stale_or_missing_telemetry_is_not_a_live_diagnosis():
    assert classify(row(),200)['confidence']=='unavailable'
    assert classify(row(running=False),100)['confidence']=='unavailable'
