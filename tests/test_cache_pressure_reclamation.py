import json
import sqlite3
import pytest
from scripts import swarm_shared_cache as cache


def test_volume_attachment_race_is_capacity_wait_not_cache_corruption(monkeypatch):
    monkeypatch.setattr(cache,'users',lambda *a:[])
    monkeypatch.setattr(cache,'inspect',lambda *a:{'Id':'owner-id','ExecIDs':None})
    calls=[]
    def command(host,args):
        calls.append(args)
        if args[1:3]==['volume','rm']:raise RuntimeError('volume is in use - [new-worker]')
        return ''
    monkeypatch.setattr(cache,'command',command)
    assert cache._remove_unreferenced({}, {'container':'owner','volume':'payload'}) is False
    assert ['docker','volume','rm','payload'] in calls


def test_terminal_cleanup_reclaims_all_safe_candidates(monkeypatch,tmp_path):
    rows=[]
    for i in range(20):
        key=f'{i:032x}'+'a'*32;rows.append({'Name':'/swarm-job-'+key[:32], 'Config':{'Labels':{cache.OWNER:key}}})
    monkeypatch.setattr(cache,'command',lambda host,args:'\n'.join(str(i) for i in range(20)) if args[1]=='ps' else json.dumps(rows))
    monkeypatch.setattr(cache,'inspect',lambda host,kind,name:{'Driver':'local','Labels':{cache.OWNER:rows[int(name[-32:],16)]['Config']['Labels'][cache.OWNER]},'Options':{'type':'tmpfs','o':'size=64m,mode=1777'}})
    removed=[];monkeypatch.setattr(cache,'_remove_unreferenced',lambda host,row:(removed.append(row['key']) or True))
    monkeypatch.setattr(cache,'finished_cache_keys',lambda host:{r['Config']['Labels'][cache.OWNER] for r in rows})
    assert cache.evict_unused({},required_mb=128,keys={r['Config']['Labels'][cache.OWNER] for r in rows})==20
    assert len(removed)==20
    with pytest.raises(ValueError):cache.evict_unused({},required_mb=0)
