from gpu_metrics import activity,GpuSampler


def sample(at,value,boot='a'):
    return {'monotonic':at,'boot_id':boot,'cards':{'pci':{}},'clients':{'pci:7':{'pci_id':'pci','engines':{'render':value},'capacity':{'render':2}}}}


def test_visible_engine_rates_account_for_capacity_and_counter_resets():
    before=sample(1,0);after=sample(3,2_000_000_000)
    assert activity(before,after)['pci']['utilization_percent']==50
    assert 'visible' in activity(before,after)['pci']['utilization_basis']
    assert activity(after,before)['pci']=={}
    assert activity(before,sample(3,2_000_000_000,'new'))['pci']=={}
    assert activity(before,sample(90,2_000_000_000))['pci']=={}


def test_driver_busy_and_shared_memory_are_measured_without_client_guessing():
    value=sample(1,0);value['cards']['pci']={'busy':17,'memory_total':8*2**30,'memory_used':2**30}
    row=activity(None,value)['pci']
    assert row['utilization_percent']==17
    assert row['memory_total_mib']==8192 and row['memory_free_mib']==7168
    assert activity(None,sample(1,0))['pci']=={}


def test_sampling_failure_preserves_existing_nvidia_metrics():
    def fail(_):raise OSError('offline')
    hardware={'gpus':[{'pci_id':'pci','utilization_percent':12}]}
    assert GpuSampler(fail).sample({},hardware)==hardware
