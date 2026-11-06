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


def test_gpu_memory_measurements_never_train_cpu_or_other_gpu_profiles():
    cpu=[memory_item('cpu'+str(i),peak=250) for i in range(5)]
    gpu=[memory_item('gpu'+str(i),peak=900,sink='gpu',gpu_uuid='GPU-a') for i in range(5)]
    pending_cpu=memory_item('new-cpu','running')
    pending_gpu=memory_item('new-gpu','running',sink='gpu',gpu_uuid='GPU-a')
    other=memory_item('other-gpu','running',sink='gpu',gpu_uuid='GPU-b')
    attach_estimates(cpu+gpu+[pending_cpu,pending_gpu,other])
    assert pending_cpu['memory_profile']['max_peak_mb']==250
    assert pending_gpu['memory_profile']['max_peak_mb']==900
    assert pending_gpu['memory_profile']['memory_domain']=='host-ram'
    assert pending_gpu['measurement_sink']=='gpu'
    assert pending_cpu['measurement_sink']=='cpu'
    assert 'memory_profile' not in other


def test_recorded_execution_class_overrides_gpu_queue_assignment():
    from runtime_estimates import measurement_sink
    fallback=memory_item('fallback',sink='cpu')
    fallback['worker']='machine-a#gpu'
    assert measurement_sink(fallback)=='cpu'


def test_backtest_keeps_chronological_history_separate_per_worker():
    rows=[]
    for i in range(40):
        for worker,elapsed in [('a#cpu',100),('b#cpu',1000)]:
            sample=item(str(i)+worker,worker=worker,elapsed=elapsed)
            sample['updated']=i+1
            rows.append(sample)
    result=attach_estimates(list(reversed(rows)))['validation']
    assert result['predictions']==74
    assert result['median_absolute_percentage_error']==0


def test_genome_estimates_separate_methods_samples_and_references():
    from runtime_estimates import attach_estimates
    def row(ident,state,group,elapsed=None):
        return {'id':ident,'run_type':'genome-analysis','kind':'regional','parent':'genomics-followup:revision',
                'estimate_class':group,'worker':'worker','status':state,'completed_at':1,
                'runtime':{'elapsed_seconds':elapsed},'execution':{}}
    group=['call-','julian','GRCh38']
    done=[row(str(i),'completed',group,100+i) for i in range(5)]
    running=row('now','running',group,10)
    other_reference=row('native','running',['call-','julian','HuRef2'],10)
    other_method=row('project','running',['project-',None,'HuRef2'],10)
    attach_estimates([*done,running,other_reference,other_method],now=1000)
    assert running['runtime_estimate']['samples']==5
    assert 'runtime_estimate' not in other_reference and 'runtime_estimate' not in other_method
