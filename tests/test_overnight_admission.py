from scripts.launcher_agent import memory_headroom
from scripts.wgs_recall import alignment


def test_measured_usage_is_not_subtracted_twice():
    limits={'memory_mb':64000,'available_mb':31000}
    active=[{'memory_mb':20000,'resident_mb':5000},{'memory_mb':10000,'resident_mb':5000}]
    assert memory_headroom(limits,active)==11000


def test_unused_reservations_and_background_pressure_stay_protected():
    assert memory_headroom({'memory_mb':16000,'available_mb':8000},[{'memory_mb':8000}])==0
    assert memory_headroom({'memory_mb':16000,'available_mb':3000},[{'memory_mb':8000,'resident_mb':5000}])==0
    assert memory_headroom({'memory_mb':16000,'available_mb':15000},[{'memory_mb':8000,'resident_mb':8000}])==8000


def test_stream_mapper_uses_bounded_batch_and_ram_sort_prefix():
    cmd=alignment('/ref/reference.fa',['/a','/b'],'/work/alignment.bam','Venter','test',2,8192,batch_bases=1000000,sort_memory_mb=128,temp_prefix='/work/tmp/sort')
    assert '-K 1000000' in cmd and '-m 128M' in cmd and '-T /work/tmp/sort' in cmd


def test_stream_commands_preserve_arguments_without_ssh_or_shell_interpolation():
    from scripts import swarm_runtime
    host={'host':'worker','local':False,'machine':'a'*64,'agent_endpoint':'http://10.0.0.2:8790','ssh':['missing-ssh']}
    command=swarm_runtime.shell_command(host,['cat','--','/source with spaces'])
    assert command[-3:]==['cat','--','/source with spaces']
    assert 'missing-ssh' not in command


def test_stream_commands_require_enrolled_identity_without_ssh_fallback():
    import pytest
    from scripts import swarm_runtime
    with pytest.raises(ValueError,match='no SSH fallback'):
        swarm_runtime.shell_command({'local':False,'ssh':['ssh','owner@worker']},['true'])
