"""An attached Android browser is a one-slot worker owned by this coordinator."""
from paths import STATE
import fcntl
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import tempfile
import threading
import time

STATE=STATE/'mobile-status.json'


def adb(*args,serial=None):
    return subprocess.check_output(['adb',*(['-s',serial] if serial else []),*args],text=True,stderr=subprocess.STDOUT,timeout=10).strip()


def gpu_history(serial):
    try:
        record=json.loads(STATE.with_name('mobile-gpu.json').read_text())
        return record if record.get('serial')==serial else {}
    except (OSError,ValueError):return {}


def discover():
    devices=[]
    try:
        for line in adb('devices','-l').splitlines()[1:]:
            parts=line.split()
            if len(parts)<2:continue
            attributes=dict(p.split(':',1) for p in parts[2:] if ':' in p)
            if attributes.get('model')!='Pixel_3':continue
            serial,status=parts[:2]
            device={'serial':serial,'name':'Pixel 3','kind':'android','transport':'USB' if 'usb' in attributes else 'ADB',
                    'status':status,'connected':status=='device','slots':1,'capabilities':['android-chrome','touch','screenshots']}
            if status=='device':
                device['android']=adb('shell','getprop','ro.build.version.release',serial=serial)
                device['gpu']={'driver':adb('shell','getprop','ro.hardware.egl',serial=serial),
                               'backend':'Browser WebGPU / WebGL; CUDA unavailable','compute_verified':False}
                previous=gpu_history(serial)
                if previous:device['gpu'].update(compute_verified=previous.get('status')=='passed',validation=previous)
                device['browser_ready']='chrome_devtools_remote' in adb('shell','cat','/proc/net/unix',serial=serial)
            devices.append(device)
        return {'devices':devices}
    except (OSError,subprocess.SubprocessError) as error:return {'devices':devices,'error':str(error)[:300]}


def view():
    result=discover()
    try:result['latest']=json.loads(STATE.read_text())
    except (OSError,ValueError):pass
    return result


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1',0))
        return sock.getsockname()[1]


class MobileRun:
    def __init__(self,service,required=False):
        self.service=service;self.host=None;self.thread=None
        devices=discover()['devices']
        device=next((d for d in devices if d['connected']),None)
        if not device and not required:return
        self.device=device
        self.host={'host':'android-'+device['serial'] if device else 'android-unavailable','name':'Pixel 3',
                   'kind':'android','local':True,'status':'queued','suites':['mobile'],'jobs':{'mobile':{
                       'name':'Pixel 3 · real Android browser','status':'queued','logs':[], 'checks':[]}},
                   'session':{'machine':device['serial'] if device else 'missing-android'},'device':device}
        with service.lock:service.run['hosts'].append(self.host);service.save()
        self.thread=threading.Thread(target=self.execute,daemon=True,name='android-browser-tests');self.thread.start()

    def save(self):
        with self.service.lock:
            self.service.save()
            STATE.parent.mkdir(exist_ok=True)
            temporary=STATE.with_suffix('.tmp')
            temporary.write_text(json.dumps({'run_id':self.service.run['id'],'host':self.host}))
            temporary.replace(STATE)

    def execute(self):
        from distributed import now
        service,host=self.service,self.host
        job=host['jobs']['mobile'];started=time.monotonic();forward=None;port=None;stay=None;lock=None;folder=None;container=None
        try:
            if not self.device:raise RuntimeError('Pixel 3 is not attached and ADB-authorized')
            serial=self.device['serial']
            lock=open(STATE.with_suffix('.lock'),'w');fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            host.update(status='running',started=now());job.update(status='running',started=now(),phase='preparing');self.save()
            folder=Path(tempfile.mkdtemp(prefix='android-',dir=service.folder.parent))
            work=folder/'OrgPortal';work.mkdir()
            repo=service.run['repositories']['OrgPortal']
            archive=subprocess.Popen(['git','-C',str(service.root/'OrgPortal'),'archive',repo['commit']],stdout=subprocess.PIPE)
            try:subprocess.run(['tar','-x','-C',str(work)],stdin=archive.stdout,check=True,timeout=60)
            finally:archive.stdout.close()
            if archive.wait(timeout=10):raise RuntimeError('Pinned source export failed')
            shutil.copyfile(Path(__file__).with_name('mobile_gpu.mjs'),work/'web/scripts/swarm-gpu-probe.mjs')
            shutil.copyfile(Path(__file__).with_name('view')/'hardware.js',work/'web/scripts/swarm-gpu-hardware.mjs')
            host['source_sync']={'OrgPortal':{'commit':repo['commit'],'status':'verified'}}
            # USB debugging remains local; the phone only sees the synthetic server.
            forward=adb('forward','tcp:0','localabstract:chrome_devtools_remote',serial=serial)
            if not forward.isdigit():raise RuntimeError('Could not allocate Chrome debugging forward')
            port=free_port();adb('reverse','tcp:'+str(port),'tcp:'+str(port),serial=serial)
            stay=adb('shell','settings','get','global','stay_on_while_plugged_in',serial=serial)
            adb('shell','settings','put','global','stay_on_while_plugged_in','3',serial=serial)
            adb('shell','input','keyevent','KEYCODE_WAKEUP',serial=serial)
            adb('shell','wm','dismiss-keyguard',serial=serial)
            adb('shell','am','start','-a','android.intent.action.VIEW','-d','about:blank','com.android.chrome',serial=serial)
            from docker_utils import ensure_image
            image=ensure_image('node:22-bookworm')
            cache=service.folder.parent/'mobile-npm-cache';cache.mkdir(exist_ok=True)
            container='orgportal-mobile-'+service.run['id']
            command=['docker','run','--rm','--pull=never','--name',container,'--network','host','--cpus','2','--memory','3g',
                     '--user',str(os.getuid())+':'+str(os.getgid()),'--cap-drop','ALL','--security-opt','no-new-privileges',
                     '-v',str(work)+':/work','-v',str(cache)+':/cache','-w','/work/web','-e','HOME=/tmp',
                     '-e','PLAYWRIGHT_CDP_URL=http://127.0.0.1:'+forward,'-e','MOBILE_SERVER_PORT='+str(port),
                     image,'timeout','900','sh','-c','npm ci --cache /cache --no-audit --no-fund && node scripts/test-android.mjs && node scripts/swarm-gpu-probe.mjs']
            job['phase']='dependencies and real-device checks';self.save()
            process=subprocess.Popen(command,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True)
            done=threading.Event()
            def cancel():
                while not done.wait(.5):
                    if service.run.get('stop_requested') or time.monotonic()-started>900:
                        subprocess.run(['docker','rm','-f',container],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=20)
                        return
            threading.Thread(target=cancel,daemon=True).start()
            try:
                for line in process.stdout:
                    with service.lock:job['logs']=(job['logs']+[line.strip()[:2000]])[-80:]
                    try:event=json.loads(line)
                    except ValueError:event={}
                    if event.get('kind')=='mobile_check':
                        with service.lock:job['checks'].append(event);job['phase']=event['name']
                    self.save()
                code=process.wait(timeout=10)
            finally:done.set();process.stdout.close()
            from visual_artifacts import publish
            publish(work/'mobile-artifacts','mobile',lambda kind,**fields:service.update(host,{'kind':kind,**fields}))
            report=work/'mobile-artifacts/mobile-report.json'
            if report.exists():
                result=json.loads(report.read_text());job['checks']=result['checks']
                if result.get('gpu'):
                    record={**result['gpu'],'serial':serial,'checked_at':now(),'run_id':service.run['id']}
                    job['gpu']=record
                    target=STATE.with_name('mobile-gpu.json');temporary=target.with_suffix('.tmp')
                    temporary.write_text(json.dumps(record));temporary.replace(target)
            if job.get('artifact_error'):raise RuntimeError('Mobile screenshot transfer failed')
            if code:raise RuntimeError('Android browser checks failed; see device report and screenshots')
            if not job['checks'] or any(c['status']!='passed' for c in job['checks']):raise RuntimeError('No complete passing Android result')
            job.update(status='passed',test_counts={'tests':len(job['checks']),'passed':len(job['checks']),'failed':0,'skipped':0})
            host['status']='passed'
        except Exception as error:
            job.update(status='stopped' if service.run.get('stop_requested') else 'failed',failure_details=[str(error)[:500]])
            host.update(status=job['status'],error=str(error)[:500])
        finally:
            if container:
                try:subprocess.run(['docker','rm','-f',container],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=20)
                except (OSError,subprocess.SubprocessError):pass
            if self.device:
                for args in ([('forward','--remove','tcp:'+forward)] if forward else [])+([('reverse','--remove','tcp:'+str(port))] if port else [])+([('shell','settings','put','global','stay_on_while_plugged_in',stay)] if stay is not None else []):
                    try:adb(*args,serial=self.device['serial'])
                    except Exception:pass
            if folder:shutil.rmtree(folder,ignore_errors=True)
            if lock:lock.close()
            job.update(finished=now(),duration_seconds=round(time.monotonic()-started,3),timing_source='worker_monotonic')
            host.update(finished=job['finished'],duration_seconds=job['duration_seconds']);self.save()

    def join(self):
        if self.thread:self.thread.join()
