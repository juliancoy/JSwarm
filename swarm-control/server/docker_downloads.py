"""Read-only Docker pull observations; never initiate pulls or read registry auth."""
import json
from pathlib import Path
import re
import subprocess
import threading
import time

REFERENCE = re.compile(r'[a-zA-Z0-9][a-zA-Z0-9._:/-]*(?:@sha256:[a-fA-F0-9]{64})?\Z')
MAX_RECORDS = 64
RETENTION = 900


def pull_reference(args):
    """Return only an explicit image reference, never arbitrary CLI arguments."""
    if not args or Path(args[0]).name != 'docker':
        return None
    words = args[1:]
    # Other contexts may point at a different machine. Do not attribute them here.
    if any(w in ('--host','-H','--context','-c') or w.startswith(('--host=','--context=','-H')) for w in words):
        return None
    while words and words[0] in ('--debug','-D'):
        words = words[1:]
    if words[:2] == ['image','pull']:
        words = words[2:]
    elif words[:1] == ['pull']:
        words = words[1:]
    else:
        return None
    positional = []
    index = 0
    while index < len(words):
        word = words[index]
        if word == '--platform':
            index += 2
            continue
        if word.startswith('--platform=') or word in ('-a','--all-tags','-q','--quiet'):
            index += 1
            continue
        if word.startswith('-'):
            return None
        positional.append(word)
        index += 1
    if len(positional) != 1:
        return None
    reference = positional[0]
    return reference if len(reference) <= 300 and REFERENCE.fullmatch(reference) and '://' not in reference else None


def active_commands(proc=Path('/proc')):
    pulls = []
    for entry in Path(proc).iterdir():
        if not entry.name.isdigit():
            continue
        try:
            if (entry/'comm').read_text().strip() != 'docker':
                continue
            with (entry/'cmdline').open('rb') as stream:
                args = [v.decode(errors='replace') for v in stream.read(16384).split(b'\0') if v]
            reference = pull_reference(args)
            if reference:
                pulls.append({'image':reference,'pid':int(entry.name),'state':'command_active',
                              'evidence':'Explicit docker pull command is running; transfer progress is not observed',
                              'host':'compai.local','scope':'Local CLI; ambient Docker daemon context is not verified'})
            if len(pulls) >= MAX_RECORDS:
                break
        except (OSError, ValueError):
            continue
    return pulls


class DockerDownloads:
    def __init__(self):
        self.lock = threading.Lock()
        self.snapshot = {'status':'unavailable','active':[],'recent':[]}
        self.updated = 0

    def view(self):
        with self.lock:
            if time.time()-self.updated > 15:
                return {'status':'unavailable','active':[],'recent':[]}
            return {**self.snapshot,'sampled_at':self.updated}

    def worker(self):
        recent = {}
        cursor = int(time.time())-60
        while True:
            now = int(time.time())
            try:
                active = active_commands()
            except OSError:
                active = []
            engine_status = 'available'
            try:
                result = subprocess.run(['docker','--host','unix:///var/run/docker.sock','events',
                    '--since',str(cursor),'--until',str(now),'--filter','type=image',
                    '--filter','event=pull','--format','{{json .}}'],
                    capture_output=True,text=True,timeout=4,check=True)
                for line in result.stdout.splitlines():
                    event = json.loads(line)
                    if not isinstance(event,dict):continue
                    actor=event.get('Actor') or {}
                    attributes=actor.get('Attributes') or {}
                    reference = attributes.get('name') or event.get('from') or event.get('id')
                    timestamp = event.get('time')
                    if not isinstance(reference,str) or len(reference)>300 or not REFERENCE.fullmatch(reference):
                        continue
                    if not isinstance(timestamp,(int,float)) or timestamp < now-RETENTION:
                        continue
                    key = (event.get('timeNano',timestamp),reference)
                    recent[key] = {'image':reference,'state':'engine_pull_event','observed_at':timestamp,
                                   'host':'compai.local','evidence':'Local Docker Engine image pull event; not an active download indicator'}
                cursor = now
            except (OSError,ValueError,subprocess.SubprocessError):
                engine_status = 'unavailable'
                cursor = max(cursor,now-60)
            recent = dict(sorted(((k,v) for k,v in recent.items() if v['observed_at']>=now-RETENTION),
                                 key=lambda item:item[1]['observed_at'],reverse=True)[:MAX_RECORDS])
            with self.lock:
                self.snapshot = {'status':'available','engine_status':engine_status,'active':active,
                                 'recent':list(recent.values()),'retention_seconds':RETENTION,
                                 'scope':'Local explicit pull commands and recent engine events. Compose, BuildKit, API clients and other hosts may be unobserved. No layer progress or per-image network rate is inferred.'}
                self.updated = time.time()
            time.sleep(3)
