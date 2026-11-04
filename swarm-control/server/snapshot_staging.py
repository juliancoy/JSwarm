"""Debounced staging of committed sources; never commit, push, test, or deploy."""
import copy
import datetime
import json
from pathlib import Path
import subprocess
import threading
import time
import uuid

from agent_client import AgentClient, CONFIG, wait_for_events
from github_sources import REPOSITORIES, github_url, source_digest


def snapshot(root):
    refs={}
    def read(name):
        path=Path(root)/name
        def git(*args):
            return subprocess.check_output(['git','-C',str(path),*args],stderr=subprocess.DEVNULL,timeout=10).decode().strip()
        commit=git('rev-parse','HEAD')
        refs[name]={'commit':commit,'url':github_url(git('remote','get-url','origin'))}
        for entry in git('ls-tree','-r',commit).splitlines():
            if entry.startswith('160000 '):
                _,child=entry.split('\t',1)
                nested=str(Path(name)/child)
                if (Path(root)/nested/'.git').exists():read(nested)
    for name in REPOSITORIES:read(name)
    return refs


class SnapshotStaging:
    def __init__(self,root):
        self.root=Path(root)
        self.lock=threading.RLock()
        self.stopped=threading.Event()
        self.state={'status':'watching','workers':{},'policy':'committed snapshots; no automatic publication'}
        self.thread=threading.Thread(target=self.watch,daemon=True,name='snapshot-staging')

    def start(self):
        if not self.thread.is_alive():self.thread.start()

    def view(self):
        with self.lock:
            state=copy.deepcopy(self.state)
            workers=list(state['workers'].values())
            if workers and all(w.get('status') in ('ready','consumed') for w in workers):state['status']='ready'
            return state

    def watch(self):
        previous=None
        while not self.stopped.wait(5):
            try:
                refs=snapshot(self.root);digest=source_digest(refs)
                if digest!=previous:
                    previous=digest
                    with self.lock:self.state.update(status='debouncing',source_sha256=digest)
                    continue
                config=json.loads(CONFIG.read_text())
                with self.lock:self.state.update(status='staging',source_sha256=digest,repositories=refs)
                # One independently progressing loop per machine. No worker blocks
                # discovery or preparation of the rest of the fleet.
                with self.lock:
                    for name,address in config['peers'].items():
                        row=self.state['workers'].get(name,{})
                        if row.get('source_sha256')==digest and row.get('status') in ('staging','ready','consumed'):continue
                        if row.get('thread_active'):continue
                        cooldown=300 if row.get('status')=='sources_ready' else 30
                        if row.get('source_sha256')==digest and time.time()-row.get('updated',0)<cooldown:continue
                        self.state['workers'][name]={'source_sha256':digest,'status':'staging','thread_active':True}
                        threading.Thread(target=self.stage,args=(name,address,refs,digest),daemon=True).start()
            except Exception as error:
                with self.lock:self.state.update(status='waiting',error=type(error).__name__+': '+str(error)[:200])

    def stage(self,name,address,refs,digest):
        client=None
        def update(**fields):
            with self.lock:self.state['workers'][name].update(fields)
        try:
            from distributed_worker import SUITES,PYTHON
            client=AgentClient({'host':name,'agent_endpoint':'http://'+address+':8790'})
            status=client.call('status')
            if not {'snapshot-stage','event-wait'}.issubset(status.get('capabilities',[])):
                update(status='waiting_for_launcher_update');return
            bases=['monitor','map','workspace','org-api','chat','web','visual','identity','identity-python','usajobs']
            images={}
            missing=[]
            for image in dict.fromkeys([PYTHON]+[SUITES[b]['image'] for b in bases]):
                result=client.call('image.inspect',{'image':image})
                if result:images[image]=result['id']
                else:missing.append(image)
            # Stage sources even without images. Installation waits for a later
            # Start to distribute them through the normal LAN-first image path.
            ident=datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'-'+uuid.uuid4().hex[:8]
            manifest={'id':ident,'prepare_only':True,'sources_only':bool(missing),'repositories':refs,'source_sha256':digest,
                      'hosts':[{'host':name,'suites':bases,'images':images,'jobs':{b:{'status':'queued'} for b in bases}}]}
            staged=client.call('source.stage',{'id':ident,'manifest':manifest,'index':0})
            ident=staged['id']
            update(job_id=ident,missing_images=missing)
            cursor=0
            while not self.stopped.is_set():
                with self.lock:superseded=self.state.get('source_sha256')!=digest
                if superseded:
                    client.call('job.stop',{'id':ident});update(status='superseded');return
                result=wait_for_events(client,ident,cursor,self.stopped.is_set)
                cursor=result['cursor']
                if result.get('events_lost'):raise RuntimeError('Preparation event history gap')
                for row in result['events']:
                    event=row['event']
                    if event.get('kind') in ('phase','source','prepared'):
                        update(phase=event.get('phase') or event.get('status') or 'prepared')
                if result['status'] not in ('running','starting'):
                    update(status='ready' if result['status']=='completed' and result.get('preparation')=='ready' else 'sources_ready' if result['status']=='completed' else result['status'],error=result.get('error'))
                    return
        except Exception as error:
            update(status='waiting',error=type(error).__name__+': '+str(error)[:250])
        finally:
            if client:client.close()
            update(thread_active=False,updated=time.time())
