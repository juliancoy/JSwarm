"""Upload minimized synthetic observations; the scoped credential grants no report reads."""
from paths import STATE
import json
from pathlib import Path
import sqlite3
import threading
import urllib.request

BASE='https://orgportal.cc/api/org/api/admin/website-observatory'


class PortalSync:
    def __init__(self):
        self.config=Path.home()/'.config/swarm-control/integrations/orgportal.json'
        self.state_path=STATE/'portal-sync.json'
        self.lock=threading.Lock();self.error=None
        try:self.state=json.loads(self.state_path.read_text())
        except (OSError,ValueError):self.state={'published':[],'command':None}

    def request(self,path,body=None):
        config=json.loads(self.config.read_text())
        request=urllib.request.Request(BASE+path,data=json.dumps(body).encode() if body is not None else None,headers={'Authorization':'Bearer '+config['token'],'Content-Type':'application/json'})
        with urllib.request.urlopen(request,timeout=15) as response:return json.load(response)

    def save(self):
        temporary=self.state_path.with_suffix('.tmp');temporary.write_text(json.dumps(self.state));temporary.replace(self.state_path)

    def tick(self,database):
        if not self.config.exists():return None
        with self.lock:
            try:
                with sqlite3.connect(database) as db:rows=db.execute('SELECT id,payload FROM runs ORDER BY id DESC').fetchall()
                published=set(self.state['published'])
                for run_id,payload in rows:
                    if run_id in published:continue
                    self.request('/collector',json.loads(payload));published.add(run_id)
                self.state['published']=sorted(published.intersection({row[0] for row in rows}));self.save()
                job=self.request('/collector');self.error=None
                return job.get('requested_at') if job.get('requested_at') and job['requested_at']!=self.state.get('command') else None
            except Exception as exc:
                self.error=type(exc).__name__;return None

    def ack(self,command):
        with self.lock:self.state['command']=command;self.save()
