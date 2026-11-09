import json
import sys
import threading
import time

import pytest
from scripts.launcher_agent import Agent,NvidiaSampler


def test_background_sampler_coalesces_readers_and_stops_its_process():
    changed=threading.Event();sampler=NvidiaSampler(changed.set);calls=[]
    def command(name):
        calls.append(name)
        code="import json,time;print(json.dumps({'sampled_at':time.time(),'output':'GPU-11111111-1111-1111-1111-111111111111, 0000:04:00.0, RTX, 12288, 8192, 0, 40'}),flush=True);time.sleep(60)"
        return [sys.executable,'-u','-c',code]
    sampler.command=command;sampler.remove=lambda _:None;sampler.reap_orphans=lambda:None
    try:
        # Starting Docker/driver observation is asynchronous for every caller.
        threads=[threading.Thread(target=sampler.snapshot) for _ in range(20)]
        for thread in threads:thread.start()
        for thread in threads:thread.join(timeout=1)
        assert all(not t.is_alive() for t in threads)
        assert changed.wait(3) and len(calls)==1
        assert sampler.snapshot()[0]['memory_free_mib']==8192
        assert sampler.health()['status']=='fresh'
        with sampler.lock:sampler.rows[0]['sampled_at']=time.time()-11
        assert sampler.snapshot()==[]  # Healthy process does not make stale data fresh.
        assert sampler.health()['status']=='stale'
    finally:sampler.close()
    assert not sampler.thread.is_alive() and sampler.rows==[]


def test_resource_wait_wakes_on_change_and_handles_node_cursor_reset():
    agent=Agent.__new__(Agent);agent.resource_condition=threading.Condition();agent.resource_revision=0;agent.storage_stop=threading.Event()
    result=[];started=threading.Event()
    def wait():started.set();result.append(agent.dispatch('resource.events',{'after':0,'timeout':2}))
    thread=threading.Thread(target=wait);thread.start();assert started.wait(1)
    agent.resource_changed();thread.join(timeout=1)
    assert result==[{'revision':1}]
    assert agent.dispatch('resource.events',{'after':100,'timeout':0})=={'revision':1}
    with pytest.raises(ValueError):agent.dispatch('resource.events',{'after':-1,'timeout':20})


def test_sampler_reaps_only_helpers_whose_container_owner_has_stopped(monkeypatch):
    from scripts import launcher_agent as module
    from types import SimpleNamespace
    sampler=NvidiaSampler();removed=[];sampler.remove=removed.append
    def run(args,**kwargs):
        if args[1]=='ps':return SimpleNamespace(stdout='dead-helper live-helper')
        owner=args[-1];return SimpleNamespace(returncode=0,stdout=json.dumps([{'State':{'Running':owner=='b'*12}}]),stderr='')
    monkeypatch.setattr(module.subprocess,'run',run)
    monkeypatch.setattr(module.subprocess,'check_output',lambda args,**kw:json.dumps([{'Config':{'Labels':{'org.lifetech.gpu-inventory-owner':('a' if args[-1]=='dead-helper' else 'b')*12}}}]))
    sampler.reap_orphans();assert removed==['dead-helper']
