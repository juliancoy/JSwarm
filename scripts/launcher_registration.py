"""Internet launcher enrollment; credentials stay in private host configuration."""
import base64
import json
import os
from pathlib import Path
import secrets
import socket
import urllib.parse
import urllib.request

from launcher_peers import CONFIG


def service_url(value):
    parsed=urllib.parse.urlsplit(value)
    if parsed.scheme!='https' or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in ('','/'):
        raise ValueError('Registration requires an HTTPS service origin')
    return value.rstrip('/')


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):
        raise ValueError('Registration redirects are not allowed')


def post(base,path,credential,body):
    request=urllib.request.Request(service_url(base)+path,data=json.dumps(body).encode(),
        headers={'Authorization':'Bearer '+credential,'Content-Type':'application/json','User-Agent':'LifeTech-Launcher/1.0'},method='POST')
    with urllib.request.build_opener(NoRedirect).open(request,timeout=30) as response:
        raw=response.read(65537)
        if len(raw)>65536:raise ValueError('Registration response too large')
        return json.loads(raw)


def save(config,path=CONFIG):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    temporary=path.with_suffix('.tmp')
    descriptor=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
    with os.fdopen(descriptor,'w') as stream:json.dump(config,stream)
    temporary.chmod(0o600);temporary.replace(path)


def register(invitation,path=CONFIG):
    value=json.loads(Path(invitation).read_text())
    if 'invitations' in value:
        if len(value['invitations'])!=1:raise ValueError('Give each device a file containing exactly one invitation')
        value=value['invitations'][0]
    base=service_url(value['url']);token=value['token']
    claims=json.loads(base64.urlsafe_b64decode(token.split('.')[1]+'==='))
    if claims['aud']!='launcher-enroll-v1':raise ValueError('Use a launcher enrollment invitation')
    # Save pending credentials before the request so a timeout can be retried safely.
    config=json.loads(Path(path).read_text()) if Path(path).exists() else {}
    pending_path=Path(path).with_suffix('.pending.json')
    pending=json.loads(pending_path.read_text()) if pending_path.exists() else {}
    existing=pending.get('internet',config.get('internet',{}))
    if existing.get('room')==claims['room'] and existing.get('peer')==claims['peer']:
        credential=existing['credential']
    else:credential=secrets.token_hex(32)
    config['internet']={'url':base,'room':claims['room'],'peer':claims['peer'],'credential':credential,'pending':True}
    save(config,pending_path)
    result=post(base,'/launchers/register',token,{'name':socket.gethostname(),'credential':credential})
    if result['room']!=claims['room'] or result['peer']!=claims['peer']:raise ValueError('Registration identity mismatch')
    config['internet'].update(pending=False,expires=result['expires'])
    save(config,path)
    pending_path.unlink(missing_ok=True)
    return {'peer':result['peer'],'room':result['room'],'expires':result['expires']}


def queue_invitation(value, config_path):
    """Validate and privately queue one invitation; the server verifies its signature."""
    import time
    if not isinstance(value,dict) or len(json.dumps(value).encode())>16384:
        raise ValueError('Use a single invitation JSON file under 16 KiB')
    if set(value)=={'invitations'} and isinstance(value['invitations'],list) and len(value['invitations'])==1:
        value=value['invitations'][0]
    if not isinstance(value,dict) or not {'url','token'}.issubset(value) or set(value)-{'url','token','peer','expires','registrationUrl'} or not isinstance(value['token'],str):
        raise ValueError('Invitation must contain only url and token')
    base=service_url(value['url'])
    try:
        claims=json.loads(base64.urlsafe_b64decode(value['token'].split('.')[1]+'==='))
        if claims.get('aud')!='launcher-enroll-v1' or claims['exp']<=time.time():raise ValueError()
        if not all(isinstance(claims.get(k),str) and claims[k] for k in ('peer','room')):raise ValueError()
    except (ValueError,KeyError,TypeError,IndexError):
        raise ValueError('Invitation is invalid or expired; request a fresh device invitation') from None
    config=json.loads(Path(config_path).read_text())
    if not all(config.get(k) for k in ('name','key','peers','internet_nodes')):
        raise ValueError('This device needs trusted swarm provisioning before internet enrollment')
    save({'url':base,'token':value['token'],'renew':True},Path(config_path).parent/'invitation.json')
