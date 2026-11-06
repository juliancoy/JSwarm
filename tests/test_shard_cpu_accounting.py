import contextlib
import io
import json
import subprocess
import sys
from types import SimpleNamespace


def test_all_memory_counters_come_from_the_worker_not_the_owner_helper(monkeypatch):
    import importlib.util
    from pathlib import Path
    spec=importlib.util.spec_from_file_location('worker_counters',Path(__file__).parents[1]/'scripts/shard_cpu_accounting.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);REMOTE=module.REMOTE
    sentinel={'cpu_core_seconds':12.5,'memory_current_bytes':12345,'memory_peak_bytes':23456,
              'memory_anon_bytes':6789,'memory_file_bytes':5556,'memory_shmem_bytes':11,'memory_swap_bytes':0}
    commands=[]
    def run(command,**kwargs):
        commands.append(command)
        return SimpleNamespace(returncode=0,stdout=json.dumps(sentinel))
    monkeypatch.setattr(subprocess,'run',run);monkeypatch.setattr(sys,'argv',['probe',json.dumps(['worker'])])
    out=io.StringIO()
    with contextlib.redirect_stdout(out):exec(compile(REMOTE,'probe','exec'),{})
    assert json.loads(out.getvalue())=={'worker':sentinel}
    assert commands[0][:5]==['docker','exec','worker','python3','-c']
    assert 'memory.peak' in commands[0][5] and '/sys/fs/cgroup' in commands[0][5]
