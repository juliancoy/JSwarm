"""Selected-node controls over the authenticated command API; no SSH fallback."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import threading
import time
from agent_client import AgentClient, CONFIG

POOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix='node-controls')
ACTION_POOL = ThreadPoolExecutor(max_workers=2, thread_name_prefix='node-actions')
LOCK = threading.Lock()
CACHE = {}
ACTIONS = {'pause', 'resume', 'auto', 'update'}

POLICY_CHANGE = r'''
import fcntl,json,os,pathlib,sys,tempfile,time
request=json.load(sys.stdin)
config=pathlib.Path(os.environ.get('LIFETECH_PEER_CONFIG',str(pathlib.Path.home()/'.config/lifetech-launcher/peers.json')))
if json.loads(config.read_text())['name']!=request['node']:raise ValueError('Node identity changed')
if __import__('hashlib').sha256(pathlib.Path('/etc/machine-id').read_bytes()).hexdigest()!=request['machine']:raise ValueError('Machine identity changed')
with (config.parent/'worker-policy.lock').open('a') as lock:
 fcntl.flock(lock,fcntl.LOCK_EX)
 path=config.parent/'worker-policy.json';marker=config.parent/'maintenance.json'
 value=json.loads(path.read_text()) if path.exists() else {'mode':'auto'}
 action=request['action']
 if action=='pause':
  if value.get('mode')!='paused':value['resume_mode']=value.get('mode','auto')
  value['mode']='paused'
 elif action in ('resume','auto'):
  if action=='resume' and marker.exists():value=json.loads(marker.read_text()).get('previous_policy',{'mode':'auto'})
  value['mode']='auto' if action=='auto' else value.pop('resume_mode','auto') if value.get('mode')=='paused' else value.get('mode','auto')
  if value['mode'] not in ('auto','always'):value['mode']='auto'
 else:raise ValueError('Unsupported policy action')
 if value.get('mode') not in ('auto','always','paused'):raise ValueError('Invalid saved policy')
 with tempfile.NamedTemporaryFile(mode='w',dir=config.parent,delete=False) as temp:
  json.dump(value,temp);temp.flush();os.fsync(temp.fileno());temporary=pathlib.Path(temp.name)
 temporary.chmod(0o600);temporary.replace(path)
 if action=='resume' and marker.exists():marker.rename(marker.with_name('maintenance-completed-'+str(time.time_ns())+'.json'))
print(json.dumps({'node':request['node'],'mode':value['mode']}))
'''


def enrolled(name):
    config = json.loads(CONFIG.read_text())
    if name not in config['peers']:
        raise ValueError('Unknown enrolled machine')
    return config, {'host': name, 'agent_endpoint': 'http://' + config['peers'][name] + ':8790'}


def project(name, status):
    if status.get('node') != name:
        raise ValueError('Node identity changed')
    policy = status.get('worker_policy') or {}
    update = status.get('update') or {}
    return {'host': name, 'observed_at': time.time(), 'connection': 'live',
            'lifecycle': status.get('lifecycle'), 'ready': status.get('ready'),
            'policy': policy.get('mode'), 'accepting_jobs': policy.get('accepting_jobs'),
            'on_battery': policy.get('on_battery'), 'reason': policy.get('reason'),
            'update_phase': update.get('phase'), 'update_failure': update.get('last_failure'),
            'revision': status.get('source_revision')}


def poll(host):
    client = None
    try:
        client = AgentClient(host)
        value = project(host['host'], client.call('status', timeout=4))
    except Exception:
        value = {'connection': 'unavailable'}
    finally:
        if client is not None:client.close()
    with LOCK:
        row = CACHE[host['host']]
        row['value'] = {**row['value'], **value}
        row.update(polling=False, checked=time.monotonic())


def snapshot(name):
    _, host = enrolled(name)
    with LOCK:
        row = CACHE.setdefault(name, {'value': {'host': name, 'connection': 'loading'},
                                     'polling': False, 'checked': 0, 'action': None})
        if not row['polling'] and time.monotonic() - row['checked'] >= 1:
            row['polling'] = True
            POOL.submit(poll, host)
        value = dict(row['value'])
        if time.time() - value.get('observed_at', 0) > 8 and value['connection'] == 'live':
            value['connection'] = 'stale'
        action = row['action']
        if action and action['status'] == 'sent' and value['connection'] == 'live' and value.get('observed_at', 0) > action['sent_at']:
            matched = value.get('revision') == action.get('revision') if action['name'] == 'update' else value.get('policy') == 'paused' if action['name'] == 'pause' else value.get('policy') == 'auto' if action['name'] == 'auto' else value.get('policy') in ('auto', 'always')
            if matched:
                action = {**action, 'status': 'observed', 'message': 'Node state confirmed.'}
                row['action'] = action
        if action and action['status'] == 'sent' and time.time() - action['sent_at'] > (240 if action['name'] == 'update' else 30):
            action = {**action, 'status': 'unconfirmed', 'message': 'Node has not confirmed the requested state. Check its current policy before retrying.'}
            row['action'] = action
        return {**value, 'action': action}


def update_payload(config, status):
    from agent_updates import LauncherUpdates, INSTALL
    release = LauncherUpdates.desired()
    removed = set(status.get('capabilities', [])) - release.capabilities
    if removed:
        raise ValueError('Published release would remove node capabilities')
    return INSTALL, {'supervisor_source': release.supervisor_source,
                     'supervisor_sha256': hashlib.sha256(release.supervisor_source.encode()).hexdigest(),
                     'machine': status['machine'], 'node': status['node'], 'revision': release.revision,
                     'files': release.files, 'controller_public_key': config['controller_public_key'],
                     'peers': config['peers']}


def execute(host, config, action):
    from task_machines import command
    client = None
    release_lock = None
    try:
        client = AgentClient(host)
        status = client.call('status', timeout=4)
        project(host['host'], status)
        if action == 'update':
            from agent_updates import deployment_lock
            release_lock = deployment_lock()
            code, payload = update_payload(config, status)
        else:
            code = POLICY_CHANGE
            payload = {'node': host['host'], 'machine': status['machine'], 'action': action}
        _, exit_code, _ = command(client, ['python3', '-c', code], json.dumps(payload).encode(), timeout=180 if action == 'update' else 20)
        if exit_code:
            raise RuntimeError('Node rejected the command; inspect its current state before retrying.')
        if action == 'update':
            deadline = time.monotonic() + 240
            while time.monotonic() < deadline:
                try:
                    observed = client.call('status', timeout=4)
                    if observed.get('machine') != status['machine']:raise ValueError('Machine identity changed during update')
                    if observed.get('source_revision') == payload['revision'] and observed.get('ready'):break
                except (OSError, RuntimeError):
                    client.close()
                time.sleep(1)
            else:raise TimeoutError('Published release readiness not confirmed')
        result = {'name': action, 'status': 'sent', 'sent_at': time.time(), 'revision': payload.get('revision'), 'message': 'Command delivered; awaiting node observation.'}
    except Exception:
        # A timed-out mutation can have succeeded. Never automatically resend it.
        result = {'name': action, 'status': 'unconfirmed', 'message': 'Command not confirmed. Check live node state before retrying.'}
    finally:
        if client is not None:client.close()
        if release_lock is not None:release_lock.close()
    with LOCK:
        CACHE[host['host']].update(action=result, checked=0)


def request(name, action):
    if action not in ACTIONS:
        raise ValueError('Unsupported machine action')
    config, host = enrolled(name)
    snapshot(name)
    with LOCK:
        row = CACHE[name]
        if row['action'] and row['action']['status'] in ('sending', 'sent'):
            raise RuntimeError('A command is already in progress for this machine')
        row['action'] = {'name': action, 'status': 'sending', 'message': 'Sending command to this node…'}
        ACTION_POOL.submit(execute, host, config, action)
    return {'host': name, 'action': action, 'accepted': True}
