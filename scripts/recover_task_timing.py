"""Recover missing historical mapping times from attempt-matched node journals.

Never change executor reports, scientific receipts, queue outcomes, or infer
runtime from queue residence. Missing evidence remains missing.
"""
import concurrent.futures
import datetime
import json
from pathlib import Path
import sqlite3
import sys
import time

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'swarm-control/server'))
from agent_client import AgentClient,CONFIG
from task_pipeline import snapshot
from scripts.launcher_streams import atomic_json


def recover():
    cfg=json.loads(CONFIG.read_text());groups={}
    for item in snapshot()['items']:
        report=item.get('execution') or {};container=report.get('container')
        if item['status']!='completed' or item.get('kind')!='paired-read-alignment' or item.get('runtime',{}).get('elapsed_seconds') is not None or not container:continue
        host=str(item.get('worker') or '').split('#')[0]
        if host in cfg['peers']:groups.setdefault(host,[]).append(item)
    def read(entry):
        host,items=entry;client=AgentClient({'host':host,'agent_endpoint':'http://'+cfg['peers'][host]+':8790'});found=[]
        try:
            client.call('status',timeout=8)
            for item in items:
                report=item['execution'];container=report['container'];job=container.rsplit('-',1)[-1]
                try:
                    reply=client.call('worker.status',{'job':job,'container':container},timeout=15)
                    value=reply if reply.get('found') else reply.get('last_observation') or {}
                    if value.get('container')!=container or value.get('job')!=job or value.get('exit_code') is None:continue
                    if report.get('image') and value.get('image')!=report['image']:continue
                    start=datetime.datetime.fromisoformat(value['started_at'].replace('Z','+00:00')).timestamp()
                    end=datetime.datetime.fromisoformat(value['finished_at'].replace('Z','+00:00')).timestamp()
                    if start<=0 or end<=start:continue
                    recovered={**report,'started':value['started_at'],'finished':value['finished_at'],'elapsed_seconds':end-start,
                               'timing_basis':'Recovered from attempt-matched durable Docker node journal; queue residence excluded'}
                    for key in ('cpu_core_seconds','cpu_sampled_at','cpu_throttled_seconds','cpu_measurement_basis','cpu_accounting_coverage'):
                        if key in value:recovered[key]=value[key]
                    found.append((item['job_id'],container,recovered))
                except (OSError,ValueError,KeyError,TypeError,RuntimeError):continue
        except (OSError,RuntimeError):pass
        finally:client.close()
        return found
    recovered=[]
    if groups:
        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
            for batch in pool.map(read,groups.items()):recovered.extend(batch)
    db_path=Path.home()/'.local/state/swarm-control/shard-runtime.sqlite'
    with sqlite3.connect(db_path,timeout=15) as db:
        db.execute('PRAGMA synchronous=FULL')
        for job,container,value in recovered:
            old=db.execute('SELECT report FROM observations WHERE job=? AND container=?',(job,container)).fetchone()
            merged={**(json.loads(old[0]) if old else {}),**value}
            db.execute('INSERT OR REPLACE INTO observations VALUES(?,?,?)',(job,container,json.dumps(merged,sort_keys=True)))
    result={'sampled_at':time.time(),'candidates':sum(map(len,groups.values())),'recovered':len(recovered),'scope':'Durable container identity and timestamps only; no reconstructed CPU estimates'}
    atomic_json(Path.home()/'.local/state/swarm-control/timing-recovery/latest.json',result)
    return result


if __name__=='__main__':print(json.dumps(recover()))
