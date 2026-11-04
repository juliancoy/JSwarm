"""Authenticated test-master backup orchestration; independent of disposable test jobs."""
from paths import WORKSPACE
import concurrent.futures
import datetime as dt
import json
from pathlib import Path
import sys
import threading
import time

ROOT=WORKSPACE
sys.path.insert(0,str(ROOT/'scripts'))
from fleet_backup import DEFAULT_CACHE,package,write_json


class FleetBackups:
    def __init__(self,tests,folder=None):
        self.tests=tests;self.folder=Path(folder or ROOT/'.runtime/fleet-backups')
        self.folder.mkdir(parents=True,exist_ok=True,mode=0o700)
        self.lock=threading.RLock();self.thread=None
        self.state=json.loads((self.folder/'latest.json').read_text()) if (self.folder/'latest.json').exists() else {'status':'never-run','devices':{}}
        if self.state['status'] in ('packing','replicating'):
            self.state['status']='interrupted';self.state['error']='Coordinator restarted; start again to resume verified parts'
    def view(self):
        with self.lock:return json.loads(json.dumps(self.state))
    def update(self,host=None,**fields):
        with self.lock:
            (self.state['devices'][host] if host else self.state).update(fields)
            self.state['updatedAt']=dt.datetime.now(dt.timezone.utc).isoformat();write_json(self.folder/'latest.json',self.state)
    def start(self):
        with self.lock:
            if self.thread and self.thread.is_alive():return {'accepted':False,**self.view()}
            self.state={'status':'packing','startedAt':dt.datetime.now(dt.timezone.utc).isoformat(),'devices':{},'message':'Preparing the latest complete DR snapshot'}
            self.thread=threading.Thread(target=self.run,daemon=True);self.thread.start()
            return {'accepted':True,**self.view()}
    def run(self):
        started=time.monotonic()
        try:
            store=Path('/mnt/PILEDRIVE/organization-dr');snapshot=store/json.loads((store/'latest.json').read_text())['snapshot']
            folder=package(snapshot,store,progress=lambda message:self.update(message=message))
            manifest=json.loads((folder/'manifest.json').read_text())
            from agent_client import CONFIG
            config=json.loads(CONFIG.read_text());recorded={h['host']:h for h in (self.tests.view().get('run') or {}).get('hosts',[])}
            hosts=[]
            for name,address in config['peers'].items():
                if not name.endswith('.local'):raise ValueError('Enrolled backup targets require mDNS identities')
                row={**recorded.get(name,{}),'host':name,'agent_endpoint':'http://'+address+':8790','local':name==config['name'],'lan_address':address}
                hosts.append(row)
            self.update(status='replicating',snapshot=snapshot.name,encryptedBytes=sum(p['size'] for p in manifest['parts']),parts=len(manifest['parts']),devices={h['host']:{'status':'queued','bytesStored':0} for h in hosts},message='Replicating encrypted archives through authenticated test agents')
            with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:list(pool.map(lambda h:self.device(h,folder,manifest),hosts))
            states=[v['status'] for v in self.state['devices'].values()]
            self.update(status='complete' if all(s=='verified' for s in states) else 'partial',durationSeconds=round(time.monotonic()-started,2),finishedAt=dt.datetime.now(dt.timezone.utc).isoformat())
        except Exception as error:
            self.update(status='failed',error=type(error).__name__+': '+str(error)[:250],durationSeconds=round(time.monotonic()-started,2))
    def device(self,host,folder,manifest):
        from agent_client import AgentClient
        from agent_updates import LauncherUpdates
        name=host['host'];started=time.monotonic();client=None
        try:
            if host.get('local'):
                receipt={'verified':True,'snapshot':manifest['snapshot'],'parts':len(manifest['parts']),'bytes':sum(p['size'] for p in manifest['parts']),'path':str(folder)}
                self.update(name,status='verified',bytesStored=receipt['bytes'],path=str(folder),receipt=receipt,durationSeconds=round(time.monotonic()-started,2))
                return
            self.update(name,status='connecting')
            client=AgentClient(host);status=client.call('status')
            if status['node']!=name:raise RuntimeError('Authenticated node identity mismatch')
            expected=host.get('session',{}).get('machine')
            if expected and status['machine']!=expected:raise RuntimeError('Physical machine identity changed')
            # Source updates wait for current tests; backups never interrupt them.
            updater=LauncherUpdates.desired(lambda:False,lambda h,p,v:self.update(name,status=p))
            deadline=time.monotonic()+1200
            while status.get('source_revision')!=updater.revision and (status.get('active_jobs') or status.get('active_image_transfers') or status.get('active_uploads')):
                self.update(name,status='waiting-for-tests');time.sleep(10);client.close();status=client.call('status')
                if time.monotonic()>deadline:raise RuntimeError('Agent still busy; retry later')
            status=updater.ensure(host,client,status)
            if 'encrypted-backup' not in status.get('capabilities',[]):raise RuntimeError('Agent does not support persistent encrypted backups')
            begin=client.call('backup.begin',{'manifest':manifest},timeout=300)
            missing=set(begin['missing']);stored=sum(p['size'] for p in manifest['parts'] if p['sha256'] not in missing)
            self.update(name,status='copying',machine=status['machine'],bytesStored=stored,path=begin['path'])
            for part in manifest['parts']:
                if part['sha256'] not in missing:continue
                # Stay below the agent's bounded 120-second replay-cache quota.
                token=client.upload(folder/(part['sha256']+'.enc'),max_bytes_per_second=24*1024**2)
                client.call('backup.put',{'snapshot':manifest['snapshot'],'sha256':part['sha256'],'upload':token},timeout=120)
                stored+=part['size'];self.update(name,bytesStored=stored)
            self.update(name,status='verifying')
            result=client.call('backup.commit',{'snapshot':manifest['snapshot']},timeout=900)
            self.update(name,status='verified',receipt=result,durationSeconds=round(time.monotonic()-started,2))
        except Exception as error:
            self.update(name,status='unavailable',error=type(error).__name__+': '+str(error)[:300],durationSeconds=round(time.monotonic()-started,2))
        finally:
            if client:client.close()
