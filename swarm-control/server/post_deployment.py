"""Bounded post-deployment checks, pinned to a distributed run's OrgPortal commit."""
import json
from pathlib import Path
import re
import time
import urllib.request
from urllib.parse import quote

CONFIG=Path.home()/'.config/map-master/post-deployment.json'

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):raise RuntimeError('Unexpected redirect')

def request(url,token=None,body=None):
    headers={'Cache-Control':'no-cache','User-Agent':'map-master-post-deployment/1.0'}
    if token:headers['Authorization']='Bearer '+token
    if body is not None:headers['Content-Type']='application/json'
    req=urllib.request.Request(url,headers=headers,data=json.dumps(body).encode() if body is not None else None)
    with urllib.request.build_opener(NoRedirect()).open(req,timeout=15) as response:
        raw=response.read(4*1024*1024)
        return raw,response.headers

def document(url,**kwargs):return json.loads(request(url,**kwargs)[0])

def execute(run,save):
    stage={'status':'waiting_for_deployment','checks':[], 'expected_commit':run['repositories']['OrgPortal']['commit']}
    run['post_deployment']=stage
    def update():
        stage['updated']=time.time();save()
    def check(name,status,detail):
        stage['checks'].append({'name':name,'status':status,'detail':detail});update()
    update()
    try:
        config=json.loads(CONFIG.read_text()) if CONFIG.exists() else {}
        origin=config.get('origin','https://orgportal-web.jcloiacon.workers.dev').rstrip('/')
        if not re.fullmatch(r'https://[A-Za-z0-9.-]+(?::443)?',origin):raise ValueError('An HTTPS origin is required')
        commit=stage['expected_commit'];deadline=time.monotonic()+600
        while True:
            try:
                rows=[document(origin+path+'?release='+commit) for path in ('/health','/api/org/health')]
                if all(row.get('ok') is True and row.get('commit')==commit and row.get('dirty') is False and row.get('service')==service for row,service in zip(rows,('orgportal-web','org-worker'))):break
            except Exception:pass
            if time.monotonic()>=deadline:
                stage['status']='deployment_not_observed';check('release','blocked','Expected clean frontend and API commits were not observed within 10 minutes');return
            time.sleep(5)
        stage['status']='running';check('release','passed','Frontend and proxied API match the tested commit')
        html=request(origin+'/')[0].decode()
        asset=re.search(r'src="(/assets/[^" ]+\.js)"',html)
        if not asset:raise RuntimeError('Frontend entry asset missing')
        _,headers=request(origin+asset[1])
        if 'javascript' not in headers.get('Content-Type',''):raise RuntimeError('Frontend entry asset is not JavaScript')
        check('frontend','passed','Page and JavaScript entry asset available')
        email=config.get('email')
        if not email:
            check('email','blocked','Configure a dedicated test campaign and bearer-token file; inbox receipt is not measured')
            stage['status']='partial';return
        token=Path(email['token_file']).read_text().strip()
        base=origin+'/api/org/api/email/campaigns/'+quote(email['campaign_id'],safe='')
        before=document(base,token=token)
        known={d['id'] for d in before.get('deliveries',[])}
        stage['email_attempted']=True;update()
        response=document(base+'/test',token=token,body={})
        if response.get('queued') is not True:raise RuntimeError('Email self-test was not queued')
        deadline=time.monotonic()+180
        while time.monotonic()<deadline:
            rows=document(base,token=token).get('deliveries',[])
            tests=[d for d in rows if d.get('id') not in known and d.get('kind')=='test']
            if any(d.get('status')=='sent' for d in tests):
                check('email','passed','Self-test accepted by provider; inbox receipt not measured');stage['status']='passed';return
            if any(d.get('status') in ('failed','bounced') for d in tests):raise RuntimeError('Email self-test delivery failed')
            time.sleep(5)
        raise RuntimeError('Email self-test did not reach provider acceptance within 3 minutes')
    except Exception as error:
        stage['status']='failed'
        # Do not retain HTTP bodies, credentials, recipient addresses or campaign contents.
        check('post_deployment','failed',type(error).__name__+': post-deployment verification failed')
    finally:
        stage['finished']=time.time();update()
