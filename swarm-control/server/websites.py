"""Versioned synthetic observations with bounded retention, independent of LAN discovery."""
import datetime
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import threading
import time
from portal_sync import PortalSync

SITES=('https://codecollective.us/','https://lifetech.fyi/')
RETENTION_DAYS=30


def summarize(run):
    origins={};links={}
    for row in run['requests']:
        node=origins.setdefault(row['origin'],{'origin':row['origin'],'requests':0,'bytes':0,'unknown_bytes':0,'errors':0,'cancelled':0,'auth_challenges':0,'blocked':0,'statuses':{},'types':{},'ips':set(),'durations':[]})
        node['requests']+=1
        node['bytes']+=row.get('bytes') or 0
        node['unknown_bytes']+=row.get('bytes') is None
        node['blocked']+=bool(row.get('blocked'))
        node['cancelled']+=bool(row.get('cancelled'))
        node['auth_challenges']+=row.get('status') in (401,403)
        node['errors']+=not row.get('blocked') and bool(row.get('error') or ((row.get('status') or 0)>=400 and row.get('status') not in (401,403)))
        if row.get('status') is not None:node['statuses'][str(row['status'])]=node['statuses'].get(str(row['status']),0)+1
        node['types'][row['type']]=node['types'].get(row['type'],0)+1
        if row.get('server_ip'):node['ips'].add(row['server_ip'])
        if row.get('duration_ms') is not None:node['durations'].append(row['duration_ms'])
    for node in origins.values():
        values=node.pop('durations');node['mean_duration_ms']=round(sum(values)/len(values),2) if values else None
        node['ips']=sorted(node['ips'])
    for edge in run.get('initiators',[]):
        if edge['source']==edge['target']:continue
        pair=(edge['source'],edge['target'])
        item=links.setdefault(pair,{**edge,'requests':0});item['requests']+=1
    return {'origins':list(origins.values()),'links':list(links.values()),'requests':len(run['requests']),
        'bytes':sum(n['bytes'] for n in origins.values()),'errors':sum(n['errors'] for n in origins.values()),
        'blocked':sum(n['blocked'] for n in origins.values()),'auth_challenges':sum(n['auth_challenges'] for n in origins.values()),'cancelled':sum(n['cancelled'] for n in origins.values())}


class WebsiteMonitor:
    def __init__(self,path):
        self.portal=PortalSync()
        self.path=Path(path);self.path.parent.mkdir(parents=True,exist_ok=True)
        self.lock=threading.Lock();self.running=False;self.error=None;self.last_attempt=0
        with sqlite3.connect(self.path) as db:
            db.execute('CREATE TABLE IF NOT EXISTS runs(id INTEGER PRIMARY KEY, site TEXT NOT NULL, observed TEXT NOT NULL, payload TEXT NOT NULL)')
            db.execute('CREATE INDEX IF NOT EXISTS site_observed ON runs(site,observed)')
            self.prune(db)

    def prune(self,db):
        cutoff=(datetime.datetime.now(datetime.timezone.utc)-datetime.timedelta(days=RETENTION_DAYS)).isoformat()
        db.execute('DELETE FROM runs WHERE observed < ?',(cutoff,))

    def save(self,run):
        if run.get('site') not in SITES:raise ValueError('Unsupported site')
        run={**run,'schema_version':1,'summary':summarize(run)}
        with sqlite3.connect(self.path) as db:
            db.execute('INSERT INTO runs(site,observed,payload) VALUES(?,?,?)',(run['site'],run['observed'],json.dumps(run)))
            self.prune(db)

    def view(self,run_id=None):
        with sqlite3.connect(self.path) as db:
            history=[{'id':r[0],'site':r[1],'observed':r[2],'summary':json.loads(r[3])['summary'],'metrics':json.loads(r[3]).get('metrics',{})} for r in db.execute('SELECT id,site,observed,payload FROM runs ORDER BY id DESC LIMIT 200')]
            runs=[]
            for site in SITES:
                row=db.execute('SELECT id,payload FROM runs WHERE site=? ORDER BY id DESC LIMIT 1',(site,)).fetchone()
                if row:runs.append({'id':row[0],**json.loads(row[1])})
            selected=None
            if run_id:
                row=db.execute('SELECT id,payload FROM runs WHERE id=?',(run_id,)).fetchone()
                if row:selected={'id':row[0],**json.loads(row[1])}
        return {'schema_version':1,'running':self.running,'error':self.error,'sites':SITES,'runs':runs,'history':history,'selected':selected,'retention_days':RETENTION_DAYS,'portal_sync_error':self.portal.error}

    def start(self):
        with self.lock:
            if self.running:return False
            self.running=True;self.error=None;self.last_attempt=time.time()
        threading.Thread(target=self._capture,daemon=True).start();return True

    def _capture(self):
        try:
            python=os.environ.get('WEBSITE_PYTHON',str(Path.home()/'global/bin/python3'))
            result=subprocess.run([python,str(Path(__file__).with_name('website_capture.py'))],capture_output=True,text=True,timeout=90)
            if result.returncode:raise RuntimeError('Browser collector failed; verify Playwright and Chromium installation')
            for run in json.loads(result.stdout):self.save(run)
        except Exception as exc:self.error=str(exc)
        finally:
            with self.lock:self.running=False

    def scheduler(self):
        while True:
            with sqlite3.connect(self.path) as db:self.prune(db)
            command=self.portal.tick(self.path)
            if command and self.start():self.portal.ack(command)
            data=self.view()
            latest=max((datetime.datetime.fromisoformat(r['observed']).timestamp() for r in data['history']),default=0)
            if time.time()-max(latest,self.last_attempt)>=3600:self.start()
            time.sleep(60)
