"""Narrow owner-authenticated Codex web service for an HTTPS tunnel.

Bind only to loopback. The tunnel publishes this service, never the test master.
A fixed test APK can be delivered; no shell, proxy, or arbitrary-file endpoints are exposed.
"""
import json
import logging
from http.cookies import SimpleCookie
import devices
import identity
import mimetypes
import os
from pathlib import Path
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from urllib.parse import urlsplit,parse_qs
import codex
from master_api import authorized

VIEW=Path(__file__).with_name('view')
ASSETS={'/':'codex.html','/codex.html':'codex.html','/codex.js':'codex.js','/codex-screenshots.js':'codex-screenshots.js','/codex.css':'codex.css','/style.css':'style.css','/test-deployment.js':'test-deployment.js',
        '/test-deployment.css':'test-deployment.css','/swarm-control':'swarm-control.html','/swarm-control.svg':'swarm-control.svg'}
ASSETS.update({'/swarm-status':'swarm-status.html','/swarm-status.js':'swarm-status.js','/swarm-status.css':'swarm-status.css','/swarm-machines.js':'swarm-machines.js'})
ASSETS.update({'/research': 'research.html', '/research.html': 'research.html', '/research.js': 'research.js', '/research.css': 'research.css'})
ASSETS.update({'/'+name: name for name in ('task-manager.html','task-manager.js','task-manager.css','tests.css','levels.js','levels-model.js','resource-history.js','cpu-report.js','snapshot-fetch.js','docker-downloads.js')})
ASSETS['/task-manager']='task-manager.html'
ASSETS.update({'/'+name:name for name in ('monitor.js','monitor.css','monitor-model.js')})
ASSETS.update({'/history':'history.html','/history.html':'history.html','/history.js':'history.js','/history-contributions.js':'history-contributions.js','/history.css':'history.css'})
ASSETS['/machine-connections.js']='machine-connections.js'
ASSETS.update({'/'+name:name for name in ('message-markdown.js','marked-vendor.js','marked-LICENSE.txt')})
ASSETS['/wgs-progress.js']='wgs-progress.js'
ASSETS.update({'/'+name: name for name in ('genome-explorer.html','genome-explorer.js','genome-explorer.css','genome-data.js','genome-comparison.js')})
ASSETS.update({'/'+name:name for name in ('codex-voice.js','codex-queue.js')})
API='/api/v1/master/codex/'

def audit_auth_failure(error,phase):
    reasons={
        'Invalid PIdP callback.':'invalid_callback_parameters',
        'PIdP callback issuer does not match.':'issuer_mismatch',
        'Restart sign-in in this browser.':'missing_browser_cookie',
        'Sign-in expired or belongs to another browser.':'expired_or_wrong_browser',
        'This PIdP account is not the CompAI owner.':'owner_mismatch',
        'PIdP token is for a different resource.':'token_binding_mismatch',
        'PIdP token expired or is not yet valid.':'token_lifetime_invalid',
    }
    # Fixed diagnostic labels only; never exception text, URLs, identities or credentials.
    reason=reasons.get(str(error),'authentication_error')
    status=getattr(error,'code',None)
    logging.warning('swarm_auth_failed phase=%s reason=%s exception=%s http_status=%s',
                    phase,reason,type(error).__name__,status if type(status) is int else 'none')

class Handler(BaseHTTPRequestHandler):
    def log_message(self,*args):pass  # Never retain credentials, URLs, or conversation text.
    def device(self):
        try:
            cookie=SimpleCookie(self.headers.get('Cookie',''));return cookie['__Host-compai_device'].value if '__Host-compai_device' in cookie else None
        except Exception:return None
    def reply(self,status,value,kind='application/json',cookie=None,location=None):
        raw=value if isinstance(value,bytes) else json.dumps(value).encode()
        self.send_response(status)
        for k,v in {'Content-Type':kind,'Content-Length':str(len(raw)),'Cache-Control':'no-store',
                    'X-Content-Type-Options':'nosniff','Referrer-Policy':'no-referrer','X-Frame-Options':'DENY',
                    'Content-Security-Policy':"default-src 'self'; script-src 'self'; connect-src 'self'; style-src 'self'; img-src 'self' data: blob: https:; frame-ancestors 'none'; object-src 'none'; base-uri 'none'; form-action 'self'",'Strict-Transport-Security':'max-age=31536000'}.items():self.send_header(k,v)
        if kind=='application/vnd.android.package-archive':self.send_header('Content-Disposition','attachment; filename="swarm-control-test.apk"')
        if cookie is not None:
            for item in cookie if isinstance(cookie,list) else [cookie]:self.send_header('Set-Cookie',item)
        if location is not None:self.send_header('Location',location)
        self.end_headers()
        try:self.wfile.write(raw)
        except (BrokenPipeError,ConnectionResetError):pass
    def handle_request(self):
        origin=self.server.public_origin
        if len(self.headers.get_all('Host',[]))!=1 or self.headers['Host']!=urlsplit(origin).netloc:
            return self.reply(403,{'error':{'message':'Host denied'}})
        if self.headers.get('Origin') not in (None,origin):return self.reply(403,{'error':{'message':'Origin denied'}})
        path=urlsplit(self.path).path
        if self.command=='GET' and path=='/health':return self.reply(200,{'service':'swarm-control','status':'ok'})
        if self.command=='GET' and path in ('/auth/login','/auth/callback'):
            try:
                if path=='/auth/login':
                    query=parse_qs(urlsplit(self.path).query,keep_blank_values=True)
                    if set(query)-{'device_label'} or any(len(v)!=1 for v in query.values()):raise ValueError('Invalid sign-in parameters')
                    target,cookie=identity.start(origin,self.device(),query.get('device_label',['Browser'])[0])
                    return self.reply(303,{},cookie=cookie,location=target)
                cookies=SimpleCookie(self.headers.get('Cookie',''))
                browser=cookies[identity.LOGIN_COOKIE].value if identity.LOGIN_COOKIE in cookies else None
                token=identity.complete(origin,parse_qs(urlsplit(self.path).query,keep_blank_values=True),browser)
                return self.reply(303,{},cookie=[
                    '__Host-compai_device='+token+'; Path=/; Secure; HttpOnly; SameSite=Strict; Max-Age='+str(devices.LIFETIME),
                    identity.LOGIN_COOKIE+'=; Path=/; Secure; HttpOnly; SameSite=Lax; Max-Age=0'],location='/')
            except Exception as error:
                audit_auth_failure(error,'login' if path=='/auth/login' else 'callback')
                return self.reply(403,{'error':{'message':'PIdP sign-in failed. Use the CompAI owner account and restart at /auth/login.'}})
        if self.command=='GET' and path in ASSETS:
            name=ASSETS[path];return self.reply(200,(VIEW/name).read_bytes(),mimetypes.guess_type(name)[0] or 'application/octet-stream')
        pairing=self.command=='POST' and path=='/logout'
        deployment=path in ('/api/v1/test-app/status','/api/v1/test-app/download')
        valid_read=self.command=='GET' and (path in ('/api/v1/swarm/wgs','/api/v1/swarm/history','/api/v1/swarm/connections','/api/v1/swarm/task-manager','/api/v1/swarm/status','/api/v1/swarm/session','/api/v1/swarm/research','/api/v1/swarm/research/jobs') or path==API+'queue' or path==API+'threads' or path.startswith(API+'threads/') or path in ('/api/v1/test-app/status','/api/v1/test-app/download'))
        valid_write=self.command=='POST' and path in (API+'commands',API+'queue',API+'queue/cancel',API+'queue/send-now',API+'transcribe','/api/v1/swarm/research/jobs','/api/v1/swarm/tests/request')
        if not (valid_read or valid_write or pairing):return self.reply(404,{'error':{'message':'Not found'}})
        if path!='/logout' and not identity.valid_session(origin,self.device()):return self.reply(401,{'error':{'message':'Sign in with PIdP to reconnect to CompAI'}})
        if path==API+'transcribe':
            from voice import transcribe,VoiceError,MAX_AUDIO,TYPES
            try:
                if self.headers.get('Origin')!=origin:return self.reply(403,{'error':{'message':'Same-origin browser required'}})
                if self.headers.get('Transfer-Encoding') or len(self.headers.get_all('Content-Length',[]))!=1:raise ValueError()
                kind=self.headers.get('Content-Type','');size=int(self.headers['Content-Length'])
                if kind.split(';')[0] not in TYPES or not 32<=size<=MAX_AUDIO:raise ValueError()
                self.connection.settimeout(20);audio=self.rfile.read(size)
                if len(audio)!=size:raise ValueError()
                return self.reply(200,transcribe(audio,kind))
            except VoiceError as error:return self.reply(error.status,{'error':{'message':str(error)}})
            except (ValueError,OSError):return self.reply(400,{'error':{'message':'Invalid audio upload'}})
        body=None
        try:
            if valid_write or pairing:
                if self.headers.get('Origin')!=origin:return self.reply(403,{'error':{'message':'Same-origin browser required'}})
                if self.headers.get('Transfer-Encoding') or len(self.headers.get_all('Content-Length',[]))!=1:raise ValueError()
                if self.headers.get('Content-Type','').split(';')[0]!='application/json':raise ValueError()
                size=int(self.headers.get('Content-Length','-1'))
                if not 0<size<=(6*1024*1024 if path in (API+'commands',API+'queue') else 65536):raise ValueError()
                self.connection.settimeout(10)
                raw=self.rfile.read(size)
                if len(raw)!=size:raise ValueError()
                body=json.loads(raw)
            if pairing:
                if path=='/logout':
                    if self.device():devices.revoke(self.device())
                    return self.reply(200,{'ok':True},cookie='__Host-compai_device=; Path=/; Secure; HttpOnly; SameSite=Strict; Max-Age=0')

            if path=='/api/v1/swarm/wgs':
                from wgs_progress import snapshot
                return self.reply(200,snapshot())
            if path=='/api/v1/swarm/connections':
                from status import connections
                try:return self.reply(200,connections())
                except (OSError,ValueError):return self.reply(503,{'error':{'message':'Machine connection feed unavailable'}})
            if path=='/api/v1/swarm/history':
                from work_history import snapshot as history_snapshot
                try:return self.reply(200,history_snapshot())
                except (OSError,ValueError):return self.reply(503,{'error':{'message':'Workload history unavailable'}})
            if path=='/api/v1/swarm/task-manager':
                from status import task_manager
                try:return self.reply(200,task_manager())
                except (OSError,ValueError):return self.reply(503,{'error':{'message':'Task Manager measurements unavailable'}})
            if path=='/api/v1/swarm/status':
                from status import snapshot
                try:return self.reply(200,snapshot())
                except (OSError,ValueError):return self.reply(503,{'error':{'message':'Local swarm coordinator unavailable'}})
            if path=='/api/v1/swarm/research/jobs':
                from status import research_jobs
                try:return self.reply(202 if self.command=='POST' else 200,research_jobs(body))
                except OSError:return self.reply(503,{'error':{'message':'Job coordinator unavailable'}})
            if path=='/api/v1/swarm/research':
                from research import snapshot
                try:return self.reply(200,snapshot())
                except (OSError,ValueError):return self.reply(503,{'error':{'message':'Research status unavailable'}})
            if path=='/api/v1/swarm/session':return self.reply(200,identity.session_info(origin,self.device()))
            if path=='/api/v1/swarm/tests/request':
                if body!={}:raise ValueError('No command parameters accepted')
                from status import request_tests
                return self.reply(202,request_tests())
            if deployment:
                from apk_delivery import artifact
                if path.endswith('/download'):
                    data,_=artifact()
                    return self.reply(200,data,'application/vnd.android.package-archive')
                if path.endswith('/status'):
                    try:_,info=artifact()
                    except (OSError,ValueError):info=None
                    return self.reply(200,{'app':info,'transport':'https','authentication':'pidp'})
            if path in (API+'queue',API+'queue/cancel',API+'queue/send-now'):
                from message_queue import manager
                try:
                    queue=manager()
                    if path.endswith('/send-now'):return self.reply(200,queue.send_now(body))
                    if path.endswith('/cancel'):return self.reply(200,queue.cancel(body))
                    if self.command=='POST':return self.reply(202,queue.submit(body))
                    return self.reply(200,queue.view(parse_qs(urlsplit(self.path).query).get('thread_id',[''])[0]))
                except (ValueError,codex.ContextError):return self.reply(400,{'error':{'message':'Invalid queue request or unavailable thread'}})
            code,value=codex.rest(self.command,path,parse_qs(urlsplit(self.path).query),body)
            self.reply(code,value)
        except ImportError:return self.reply(503,{'error':{'message':'P2P deployment dependencies are not installed on CompAI'}})
        except PermissionError:return self.reply(401,{'error':{'message':'Device session expired'}})
        except (ValueError,UnicodeDecodeError,TimeoutError):self.reply(400,{'error':{'message':'Invalid request body'}})
    do_GET=handle_request
    do_POST=handle_request

def pairing_payload(origin):
    import base64,qrcode,qrcode.image.svg,time
    url=origin+'/auth/login'
    svg=qrcode.make(url,image_factory=qrcode.image.svg.SvgPathFillImage).to_string()
    return {'url':url,'expires_at':None,'method':'pidp','qr':'data:image/svg+xml;base64,'+base64.b64encode(svg).decode()}


class LocalPairingHandler(Handler):
    """Coordinator-only issuer. Test containers cannot use loopback as authority."""
    def do_GET(self):self.reply(405,{'error':{'message':'POST required'}})
    def do_POST(self):
        if self.path!='/pairing':return self.reply(404,{'error':{'message':'Not found'}})
        if self.headers.get_all('Host',[])!=['127.0.0.1:8092'] or self.headers.get('Origin') is not None:
            return self.reply(403,{'error':{'message':'Native coordinator request required'}})
        if not authorized(self.headers,'pairing'):return self.reply(401,{'error':{'message':'Pairing issuer credential required'}})
        try:
            if self.headers.get('Transfer-Encoding') or self.headers.get('Content-Type')!='application/json':raise ValueError()
            if len(self.headers.get_all('Content-Length',[]))!=1:raise ValueError()
            size=int(self.headers['Content-Length'])
            if not 0<size<=16:raise ValueError()
            self.connection.settimeout(5)
            if json.loads(self.rfile.read(size))!={}:raise ValueError()
            self.reply(200,pairing_payload(self.server.public_origin))
        except (ValueError,TimeoutError):self.reply(400,{'error':{'message':'Invalid pairing request'}})


if __name__=='__main__':
    import sys
    if len(sys.argv)>1 and sys.argv[1]=='revoke-devices':
        devices.revoke();print('All paired devices revoked');sys.exit(0)
    if len(sys.argv)>1 and sys.argv[1]=='pair':
        origin=os.getenv('CODEX_PUBLIC_ORIGIN','https://compai.lifetech.fyi')
        path=Path.home()/'.config/map-master/codex-pairing-link'
        fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
        with os.fdopen(fd,'w') as stream:stream.write(origin+'/auth/login\n')
        path.chmod(0o600);print('PIdP sign-in link saved to '+str(path));sys.exit(0)
    origin=os.environ['CODEX_PUBLIC_ORIGIN']
    parsed=urlsplit(origin)
    if parsed.scheme!='https' or not parsed.hostname or parsed.path or parsed.query or parsed.fragment or parsed.username:raise ValueError('Configure one HTTPS origin')
    server=ThreadingHTTPServer(('127.0.0.1',8091),Handler);server.daemon_threads=True;server.public_origin=origin
    import threading
    issuer=ThreadingHTTPServer(('127.0.0.1',8092),LocalPairingHandler);issuer.daemon_threads=True;issuer.public_origin=origin
    threading.Thread(target=issuer.serve_forever,daemon=True).start()
    from message_queue import manager
    manager(start_worker=True)
    server.serve_forever()
