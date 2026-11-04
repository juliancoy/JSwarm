from runtime_estimates import attach_estimates


def item(name,status='completed',worker='a#cpu',elapsed=100,units=10):
    return dict(id=name,status=status,worker=worker,run_type='mapping',kind='paired-read-alignment',generation='abc',runtime={'elapsed_seconds':elapsed},work_units=units,execution={'resources':{'cpus':2}},updated=1)


def test_fallback_survives_configuration_change_and_normalizes_reads():
    old=item('old');new=item('new','running',elapsed=5,units=20)
    new['execution']['resources']['memory_mb']=1408
    attach_estimates([old,new],now=10)
    assert new['runtime_estimate']['typical_seconds']==200
    assert new['runtime_estimate']['remaining_seconds']==195
    assert new['runtime_estimate']['confidence']=='low'


def test_cpu_gpu_and_reference_generations_do_not_mix():
    old=item('old');gpu=item('gpu','running',worker='a#gpu');other=item('other','running');other['generation']='def'
    attach_estimates([old,gpu,other])
    assert 'runtime_estimate' not in gpu
    assert 'runtime_estimate' not in other


def test_overrun_is_explicit_and_never_negative():
    old=item('old');new=item('new','running',elapsed=300)
    attach_estimates([old,new])
    assert new['runtime_estimate']['overrun']
    assert new['runtime_estimate']['extrapolated']
    assert new['runtime_estimate']['remaining_seconds']>0


def test_failed_and_missing_timings_never_become_samples():
    rows=[item('failed','failed'),item('unknown',elapsed=None),item('new','running')]
    attach_estimates(rows)
    assert 'runtime_estimate' not in rows[-1]


def memory_item(name,status='completed',peak=300,**overrides):
    context=dict(schema='alignment-memory.v1',machine='machine-a',image='image-a',reference_key='reference-a',reference_layout='node-shared-ram',backend='bwa',batch_bases=1000000,sort_memory_mb=128,cpus=2,pairs=500000,input_bytes=40000000,scratch_mb=512,baseline_configuration=True)
    import hashlib
    context['input_fingerprint']=hashlib.sha256(name.encode()).hexdigest()
    context.update(overrides)
    return dict(item(name,status),profiling_context=context,peak_memory_bytes=peak*1024**2,output_verified=True)


def test_memory_profiles_keep_shared_cache_separate_and_recommend_guarded_ceiling():
    rows=[memory_item(str(i),peak=p) for i,p in enumerate([250,275,300,350,438])]
    pending=memory_item('new','running');rows.append(pending)
    attach_estimates(rows)
    p=pending['memory_profile']
    assert p['samples']==5 and p['median_peak_mb']==300 and p['max_peak_mb']==438
    assert p['qualified'] and p['recommended_limit_mb']==896 and p['shared_cache_separate']


def test_memory_profiles_never_extrapolate_tiny_samples_or_mix_settings():
    rows=[memory_item(str(i),pairs=20000,input_bytes=1000000) for i in range(5)]
    full=memory_item('full','running');other=memory_item('other','running',batch_bases=250000)
    attach_estimates(rows+[full,other])
    assert not full['memory_profile']['qualified']
    assert full['memory_profile']['recommended_limit_mb'] is None
    assert 'memory_profile' not in other


def test_memory_profiles_reject_unverified_peaks_and_failures_block_tuning():
    rows=[memory_item(str(i)) for i in range(5)]
    unverified=memory_item('unverified',peak=900);unverified['output_verified']=False
    failed=memory_item('failure','failed',peak=1000)
    pending=memory_item('pending','running')
    attach_estimates(rows+[unverified,failed,pending])
    assert pending['memory_profile']['samples']==5
    assert pending['memory_profile']['max_peak_mb']==300
    assert not pending['memory_profile']['qualified']


def test_known_changed_alignment_configuration_cannot_qualify_a_limit():
    rows=[memory_item(str(i)) for i in range(5)]
    rows[-1]['configuration_equivalent']=False
    attach_estimates(rows)
    assert all(r['memory_profile']['recommended_limit_mb'] is None for r in rows)


def test_repeated_trials_on_one_shard_cannot_inflate_qualification():
    rows=[memory_item(str(i),input_fingerprint='a'*64,peak=250+i*10) for i in range(6)]
    attach_estimates(rows)
    profile=rows[0]['memory_profile']
    assert profile['observations']==6 and profile['independent_inputs']==1
    assert profile['max_peak_mb']==300
    assert profile['recommended_limit_mb'] is None
