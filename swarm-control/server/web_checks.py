"""Bounded web checks executed on this host and SSH-accessible Ethernet peers."""
import concurrent.futures
import json
import subprocess
import threading
import datetime

REMOTE = r'''
import datetime, http.client, json, os, shutil, subprocess, tempfile, time
host = HOST
local = LOCAL
remote = REMOTE
result = {'checked': datetime.datetime.now(datetime.timezone.utc).isoformat()}
try:
    if remote:
        neighbors=json.loads(subprocess.check_output(['ip','-j','-6','neigh']))
        iface=next(n['dev'] for n in neighbors if n['dst']==host)
        host += '%' + iface
    def get(path):
        c=http.client.HTTPConnection(host,80,timeout=12)
        start=time.perf_counter();c.request('GET',path);r=c.getresponse();body=r.read();elapsed=time.perf_counter()-start;c.close()
        if r.status!=200: raise RuntimeError('HTTP '+str(r.status))
        return body,elapsed
    page,_=get('/')
    assert b'LAN ecosystem' in page and b'id="graph"' in page
    devices,_=get('/api/devices')
    assert isinstance(json.loads(devices)['devices'],list)
    result['http']='passed'
    if remote:
        samples=[];total_bytes=0;total_time=0
        for _ in range(3):
            body,elapsed=get('/api/test-payload')
            assert len(body)==4194304
            samples.append(round(len(body)*8/elapsed/1e6,2));total_bytes+=len(body);total_time+=elapsed
        result['transfer']={'mbps':round(total_bytes*8/total_time/1e6,2),'samples_mbps':samples,'bytes':total_bytes,'seconds':round(total_time,3),'path':'Direct LAN IPv6 HTTP download; 3 × 4 MiB'}
    chrome=shutil.which('google-chrome') or shutil.which('chromium')
    if chrome:
        with tempfile.TemporaryDirectory(prefix='lan-web-check-') as profile:
            url='http://127.0.0.1:18088' if remote else 'http://'+local
            p=subprocess.run([chrome,'--headless','--no-sandbox','--disable-gpu','--disable-dev-shm-usage','--no-first-run','--user-data-dir='+profile,'--virtual-time-budget=5000','--dump-dom',url],capture_output=True,text=True,timeout=30)
            result['browser']='passed' if p.returncode==0 and 'data-ready="true"' in p.stdout and 'class="device' in p.stdout else 'failed'
            result['browser_path']='SSH reverse tunnel' if remote else 'Local HTTP'
    else:
        result['browser']='unavailable: Chrome is not installed'
    result['status']='passed' if result.get('browser')=='passed' else 'partial'
except Exception as exc:
    result['status']='failed';result['error']=str(exc)
print(json.dumps(result))
'''


class WebChecks:
    def __init__(self, snapshot):
        self.snapshot = snapshot
        self.lock = threading.Lock()
        self.running = False
        self.results = {}

    def view(self):
        with self.lock:
            return {'running': self.running, 'results': dict(self.results)}

    def start(self):
        with self.lock:
            if self.running:
                return
            self.running = True
        threading.Thread(target=self.run, daemon=True).start()

    def run(self):
        try:
            data = self.snapshot()
            devices = data.get('devices', [])
            with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
                list(pool.map(lambda d: self.check(d, data), devices))
        finally:
            with self.lock:
                self.running = False

    def check(self, device, data):
        key = device.get('mac') or device['ip']
        def save(result):
            with self.lock:
                self.results[key] = result
        if not device['self'] and 22 not in device['ports']:
            save({'status': 'unavailable', 'error': 'No SSH service on this device', 'checked': datetime.datetime.now(datetime.timezone.utc).isoformat()})
            return
        save({'status': 'running'})
        try:
            if device['self']:
                script = REMOTE.replace('host = HOST', 'host = '+json.dumps(data['gateway'])).replace('local = LOCAL', 'local = '+json.dumps(data['gateway'])).replace('remote = REMOTE', 'remote = False')
                p = subprocess.run(['/usr/bin/python3', '-'], input=script, capture_output=True, text=True, timeout=65)
            else:
                links=json.loads(subprocess.check_output(['ip','-j','-6','addr','show','dev',data['interface']]))
                host=next(a['local'] for a in links[0]['addr_info'] if a['scope']=='link')
                script=REMOTE.replace('host = HOST','host = '+json.dumps(host)).replace('local = LOCAL','local = '+json.dumps(data['gateway'])).replace('remote = REMOTE','remote = True')
                address=device.get('ssh_ip',device['ip'])
                dest=address+('%'+data['interface'] if ':' in address else '')
                p=None
                for user in ('julian','wolf'):
                    p=subprocess.run(['ssh','-o','BatchMode=yes','-o','ConnectTimeout=4','-o','StrictHostKeyChecking=yes','-o','ExitOnForwardFailure=yes','-R','127.0.0.1:18088:'+data['gateway']+':80',user+'@'+dest,'python3 -'],input=script,capture_output=True,text=True,timeout=70)
                    if p.returncode==0:
                        break
                if p.returncode:
                    raise RuntimeError(p.stderr.strip()[-400:] or 'SSH check failed')
            if p.returncode:
                raise RuntimeError(p.stderr.strip()[-400:] or 'Web check failed')
            save(json.loads(p.stdout.strip().splitlines()[-1]))
        except Exception as exc:
            save({'status':'failed','error':str(exc),'checked':datetime.datetime.now(datetime.timezone.utc).isoformat()})
