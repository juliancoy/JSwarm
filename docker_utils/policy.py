"""One LAN-first policy for Docker CLI and SDK operations."""
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import threading

from .images import ImageResolver

LOCK = threading.RLock()


def inventory():
    path=Path(os.getenv('DOCKER_UTILS_INVENTORY',str(Path.home()/'.local/state/orgportal/lan-ssh-devices.json')))
    if not path.exists():return []
    accounts=json.loads(path.read_text()).get('accounts',{})
    hosts=[]
    for host,user in sorted(accounts.items()):
        if not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.local',host) or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_.-]*',user):
            raise ValueError('Invalid saved LAN Docker account')
        hosts.append({'host':host,'user':user})
    if len(hosts)>24:raise ValueError('At most 24 LAN peers are supported')
    return hosts


def ssh(host):
    if host.get('local'):return ['bash','-c']
    key=os.getenv('DOCKER_UTILS_SSH_KEY',str(Path.home()/'.ssh/orgportal_lan_ed25519'))
    return ['ssh','-i',key,'-o','BatchMode=yes','-o','IdentitiesOnly=yes','-o','StrictHostKeyChecking=yes',
            '-o','ConnectTimeout=8','-o','ForwardAgent=no','-o','UserKnownHostsFile='+os.getenv('DOCKER_UTILS_KNOWN_HOSTS',str(Path.home()/'.ssh/known_hosts')),host['user']+'@'+host['host']]


def resolver():
    return ImageResolver(inventory(),ssh)


def ensure_image(image):
    with LOCK:
        service=resolver()
        service.ensure(image,[None])
        return service.host_images['local'][image]


def prepare_build(dockerfile,buildargs=None):
    """Resolve every external FROM/COPY --from image before invoking the builder."""
    args=dict(buildargs or {})
    stages=set()
    resolved={}
    text=re.sub(r'\\\s*\n',' ',str(dockerfile))
    def expand(value):
        value=re.sub(r'\$\{([A-Za-z_][A-Za-z0-9_]*)\}|\$([A-Za-z_][A-Za-z0-9_]*)',
            lambda match:str(args.get(match[1] or match[2],match[0])),value)
        if '$' in value:raise ValueError('Unresolved Dockerfile image argument: '+value)
        return value
    for raw in text.splitlines():
        line=raw.strip()
        if not line or line.startswith('#'):continue
        instruction=line.split(None,1)[0].upper()
        if instruction not in ('ARG','FROM','COPY','RUN'):continue
        if instruction=='RUN' and '--mount' in line:raise ValueError('BuildKit RUN mounts require a separate policy; this builder uses the engine image cache')
        if instruction=='RUN':continue
        parts=shlex.split(line,comments=True)
        if not parts:continue
        instruction=parts[0].upper()
        if instruction=='ARG':
            name,_,default=parts[1].partition('=')
            if name not in args and default:args[name]=expand(default)
        if instruction=='FROM':
            if any(token.startswith('--platform') for token in parts):raise ValueError('Cross-platform FROM is not supported by the native LAN cache policy')
            tokens=[token for token in parts[1:] if not token.startswith('--')]
            image=expand(tokens[0])
            if image.lower()!='scratch' and image.lower() not in stages:resolved[tokens[0]]=ensure_image(image)
            if len(tokens)>2 and tokens[1].upper()=='AS':stages.add(tokens[2].lower())
        if instruction in ('COPY','RUN'):
            for match in re.finditer(r'--(?:from|mount=type=bind,from)=([^\s,]+)',line):
                image=expand(match[1])
                if image.lower() not in stages and not image.isdigit():resolved[match[1]]=ensure_image(image)

    # Return a Dockerfile with immutable external references. The classic builder uses
    # the preloaded engine cache and cannot silently fetch a base in a separate BuildKit store.
    for image,identifier in sorted(resolved.items(),key=lambda item:-len(item[0])):
        text=re.sub(r'(?<![A-Za-z0-9_./:@-])'+re.escape(image)+r'(?![A-Za-z0-9_./:@-])',identifier,text)
    return text


def build_image(tag,context='.',dockerfile='Dockerfile',buildargs=None):
    context=Path(context).resolve()
    path=Path(dockerfile)
    if not path.is_absolute():path=context/path
    prepared=prepare_build(path.read_text(),buildargs)
    command=['docker','build','--pull=false','-t',tag,'-f','-']
    for name,value in (buildargs or {}).items():command+=['--build-arg',str(name)+'='+str(value)]
    subprocess.run(command+[str(context)],input=prepared,text=True,check=True,env={**os.environ,'DOCKER_BUILDKIT':'0'})
    return tag
