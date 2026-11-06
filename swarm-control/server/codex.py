"""Private local VS Code Codex history and owner-directed IPC.

The follower protocol is internal to the installed OpenAI VS Code extension
(26.51007). Fail closed when unavailable; never resume a second Codex process.
"""
from paths import WORKSPACE
from conversation_connection import ConnectionRecovery
import json
import base64
import binascii
import os
from pathlib import Path
import re
import select
import shutil
import socket
import sqlite3
import stat
import struct
import subprocess
import threading
import time
import uuid
from collections import deque
from contextlib import closing

HOME = Path.home() / '.codex'
SEND_LOCK = threading.Lock()
RECEIPTS = {}


def new_thread():
    """Create an empty, durable chat; VS Code owns all subsequent turns."""
    binaries = sorted(Path.home().glob('.vscode/extensions/openai.chatgpt-*/bin/linux-*/codex'))
    binary = str(binaries[-1]) if binaries else shutil.which('codex')
    if not binary:
        raise ContextError('Install Codex on this machine before creating a chat.')
    cwd = str(Path(os.getenv('MAP_CODEX_WORKSPACE', str(WORKSPACE))).resolve())
    process = subprocess.Popen([binary, 'app-server'], stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0)
    def request(ident, method, params):
        process.stdin.write((json.dumps({'id': ident, 'method': method, 'params': params}) + '\n').encode())
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if not select.select([process.stdout], [], [], max(0, deadline - time.monotonic()))[0]:
                break
            raw = process.stdout.readline(1024 * 1024)
            if not raw or not raw.endswith(b'\n'):
                break
            reply = json.loads(raw)
            if reply.get('id') == ident:
                if 'error' in reply:
                    raise ContextError('Codex could not create the conversation.')
                return reply['result']
        raise ContextError('Creating the conversation timed out. Refresh before trying again.')
    try:
        request(1, 'initialize', {'clientInfo': {'name': 'compai_remote', 'version': '1.0'}})
        result = request(2, 'thread/start', {'cwd': cwd, 'ephemeral': False, 'threadSource': 'vscode',
                                          'approvalPolicy': 'never', 'sandbox': 'danger-full-access'})
        ident = result['thread']['id']
        # Naming persists an otherwise empty thread, without starting a model turn.
        request(3, 'thread/name/set', {'threadId': ident, 'name': 'New conversation'})
    finally:
        process.terminate()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=2)
        process.stdin.close()
        process.stdout.close()
    thread(ident)  # Require durable history before reporting success.
    message = 'Conversation created. Connecting to local VS Code…'
    try:
        subprocess.run(['code', '--open-url', f'vscode://openai.chatgpt/local/{ident}'],
                       check=True, timeout=10, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        message = 'Conversation created. Use Open in local VS Code to connect it.'
    return {'accepted': True, 'thread_id': ident, 'message': message}


class ContextError(Exception):
    pass


class NotDispatched(ContextError):
    """The message never reached a turn-start/steer request and may safely wait."""
    pass


class IPC:
    def __enter__(self):
        path = HOME / 'ipc/ipc.sock'
        for p in (path.parent, path):
            info = p.stat()
            if info.st_uid != os.getuid() or info.st_mode & 0o022:
                raise ContextError('Local Codex socket must be owned by this user and private.')
        if not stat.S_ISSOCK(path.stat().st_mode):
            raise ContextError('VS Code Codex is not connected.')
        self.socket = socket.socket(socket.AF_UNIX)
        self.socket.settimeout(12)
        try:
            self.socket.connect(str(path))
            self.client = self.request('initialize', {'clientType': 'map-dashboard'})['result']['clientId']
        except Exception:
            self.socket.close()
            raise
        return self

    def __exit__(self, *args):
        self.socket.close()

    def read(self, size):
        result = bytearray()
        while len(result) < size:
            chunk = self.socket.recv(size - len(result))
            if not chunk:
                raise ContextError('VS Code disconnected.')
            result.extend(chunk)
        return bytes(result)

    def write(self, value):
        data = json.dumps(value).encode()
        self.socket.sendall(struct.pack('<I', len(data)) + data)

    def request(self, method, params, version=0, target=None):
        ident = str(uuid.uuid4())
        self.write({'type': 'request', 'requestId': ident, 'method': method,
                    'params': params, 'version': version, 'timeoutMs': 10000,
                    'sourceClientId': getattr(self, 'client', 'initializing-client'),
                    **({'targetClientId': target} if target else {})})
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            self.socket.settimeout(max(.1, deadline - time.monotonic()))
            size = struct.unpack('<I', self.read(4))[0]
            if size > 32 * 1024 * 1024:
                raise ContextError('Codex response exceeded the size limit.')
            reply = json.loads(self.read(size))
            if reply.get('type') == 'client-discovery-request':
                self.write({'type': 'client-discovery-response', 'requestId': reply['requestId'],
                            'response': {'canHandle': False}})
            if reply.get('type') == 'response' and reply.get('requestId') == ident:
                if reply.get('resultType') != 'success':
                    raise ContextError(str(reply.get('error', 'Codex request failed')))
                return reply
        raise ContextError('Codex response timed out. Check the thread before retrying.')

    def owner(self, ident):
        return self.request('thread-owner-discovery', {'hostId': 'local', 'conversationId': ident}, 1)['handledByClientId']


def connection():
    db = sqlite3.connect(f'file:{HOME / "state_5.sqlite"}?mode=ro', uri=True, timeout=2)
    db.row_factory = sqlite3.Row
    return db


def indexed_titles():
    """Read Codex's append-only display-name index without changing its state."""
    titles = {}
    try:
        with (HOME / 'session_index.jsonl').open(encoding='utf-8') as stream:
            for line in stream:
                try:
                    item = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(item, dict):
                    continue
                ident, name = item.get('id'), item.get('thread_name')
                if isinstance(ident, str) and isinstance(name, str) and name.strip():
                    titles[ident] = name
    except (OSError, UnicodeError):
        pass
    return titles


def display_title(row, titles):
    # A persisted thread name is authoritative; the index supplies names not
    # yet reflected in the DB. The raw title is often the initial prompt.
    for name in (row.get('name'), titles.get(row['id']), row.get('title')):
        if isinstance(name, str) and name.strip():
            return name
    return 'Untitled conversation'


def thread(ident):
    if not isinstance(ident, str) or not re.fullmatch(r'[a-f0-9-]{36}', ident):
        raise ContextError('Invalid thread ID.')
    with closing(connection()) as db:
        row = db.execute('SELECT id, title, name, cwd, updated_at, source, model, rollout_path FROM threads WHERE id=?', (ident,)).fetchone()
    if not row:
        raise ContextError('Thread not found.')
    result = dict(row)
    result['display_title'] = display_title(result, indexed_titles())
    return result


def history(row):
    path = Path(row['rollout_path']).resolve()
    if not path.is_relative_to(HOME.resolve()):
        raise ContextError('Thread history is outside the local Codex directory.')
    messages = deque(maxlen=60)
    activity = 'unknown'
    turn_id = None
    session_meta = False
    size = path.stat().st_size
    count = 0
    # Walk backwards over tool output until the latest 60 actual messages are
    # found. Memory is bounded even when a tool emits a very large JSON record.
    with path.open('rb') as stream:
        position = size
        pending = b''
        oversized = False
        def records():
            nonlocal position, pending, oversized
            while position:
                length = min(position, 65536)
                position -= length
                stream.seek(position)
                parts = (stream.read(length) + pending).split(b'\n')
                pending = parts.pop(0)
                for line in reversed(parts):
                    if oversized:
                        oversized = False
                        continue
                    if line and len(line) <= 512 * 1024:
                        yield line
                if len(pending) > 512 * 1024:
                    pending = b''
                    oversized = True
            if pending and not oversized:
                yield pending
        for line in records():
            try:
                item = json.loads(line)
            except (ValueError, UnicodeDecodeError):
                continue
            payload = item.get('payload', {})
            if item.get('type') == 'session_meta':
                session_meta = True
            kind = payload.get('type')
            if item.get('type') == 'event_msg' and activity == 'unknown':
                if kind == 'task_started':
                    activity, turn_id = 'running', payload.get('turn_id')
                elif kind in ('task_complete', 'task_completed', 'turn_aborted'):
                    activity, turn_id = 'idle', payload.get('turn_id')
            if item.get('type') == 'response_item' and kind == 'message' and payload.get('role') in ('user', 'assistant'):
                text = '\n'.join(part.get('text', '') for part in payload.get('content', []) if part.get('type') in ('input_text', 'output_text'))
                if text:
                    count += 1
                    if count <= 60:
                        messages.appendleft({'role': payload['role'], 'text': text[:12000], 'at': item.get('timestamp')})
                    else:
                        break
    if activity == 'unknown' and session_meta and not messages and size <= 2 * 1024 * 1024:
        activity = 'idle'
    return {'messages': list(messages), 'activity': activity, 'turn_id': turn_id,
            'history_limited': count > 60}


def listing(offset=0):
    with closing(connection()) as db:
        rows = db.execute('SELECT id, title, name, cwd, updated_at, source, model, rollout_path FROM threads ORDER BY updated_at DESC LIMIT 21 OFFSET ?', (offset,)).fetchall()
    result = []
    titles = indexed_titles()
    for raw in rows[:20]:
        row = dict(raw)
        row['display_title'] = display_title(row, titles)
        try:
            state = history(row)
            row['activity'] = state['activity']
        except OSError:
            row['activity'] = 'unknown'
        row.pop('rollout_path')
        result.append(row)
    return {'threads': result, 'next_offset': offset + 20 if len(rows) > 20 else None,
            'machine': socket.gethostname(), 'connected': (HOME / 'ipc/ipc.sock').exists()}



def open_existing_conversation(ident):
    thread(ident)  # Only an existing durable conversation; no second app-server.
    subprocess.run(['code', '--open-url', f'vscode://openai.chatgpt/local/{ident}'],
                   check=True,timeout=10,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)


CONNECTION_RECOVERY=ConnectionRecovery(open_existing_conversation)


def request_connection(ident,retry=False):
    thread(ident)
    return CONNECTION_RECOVERY.request(ident,retry=retry)


def detail(ident):
    row = thread(ident)
    row.update(history(row))
    row.pop('rollout_path')
    try:
        with IPC() as client:
            owner = client.owner(ident)
            row['owner_connected'] = True
            row['connection_state'] = 'connected'
            CONNECTION_RECOVERY.connected(ident)
            try:
                row['ide_context'] = client.request('ide-context', {'workspaceRoot': row['cwd']}, target=owner)['result'].get('ideContext')
            except (ContextError, OSError):
                row['ide_context'] = None
    except (ContextError, OSError) as error:
        row['owner_connected'] = False
        row['connection_state'] = CONNECTION_RECOVERY.status(ident) if isinstance(error,ContextError) and str(error)=='no-client-found' else 'unavailable'
    return row


def full_access_turn_settings(ident):
    """Keep remote full-access turns unattended without widening any sandbox."""
    try:
        with closing(connection()) as db:
            row = db.execute('SELECT sandbox_policy FROM threads WHERE id=?', (ident,)).fetchone()
        policy = json.loads(row['sandbox_policy']) if row else None
    except (sqlite3.Error, ValueError, TypeError):
        return {}
    if isinstance(policy, dict) and policy.get('type') in ('disabled', 'danger-full-access', 'dangerFullAccess'):
        return {'approvalPolicy': 'never', 'approvalsReviewer': 'user'}
    return {}


def screenshot_inputs(images):
    if not isinstance(images,list) or len(images)>4:
        raise ContextError('Attach up to 4 screenshots.')
    inputs=[]
    total=0
    for image in images:
        if not isinstance(image,dict) or set(image)!={'name','data_url'} or not isinstance(image['name'],str) or not 1<=len(image['name'])<=120:
            raise ContextError('Invalid screenshot attachment.')
        value=image['data_url']
        if not isinstance(value,str) or len(value)>3*1024*1024:
            raise ContextError('Screenshot exceeds the size limit.')
        match=re.fullmatch(r'data:image/(png|jpeg|webp);base64,([A-Za-z0-9+/=]+)',value)
        if not match:
            raise ContextError('Screenshots must be PNG, JPEG or WebP image data.')
        try:
            data=base64.b64decode(match[2],validate=True)
        except (ValueError,binascii.Error):
            raise ContextError('Invalid screenshot image data.')
        kind=match[1]
        valid=(kind=='png' and data.startswith(b'\x89PNG\r\n\x1a\n') or kind=='jpeg' and data.startswith(b'\xff\xd8\xff') or kind=='webp' and data.startswith(b'RIFF') and data[8:12]==b'WEBP')
        total+=len(data)
        if not valid or len(data)<32 or len(data)>2*1024*1024 or total>4*1024*1024:
            raise ContextError('Use valid screenshots up to 2 MB each and 4 MB total.')
        inputs.append({'type':'image','url':value})
    return inputs


def mutate(body):
    if not isinstance(body, dict) or set(body) - {'thread_id', 'action', 'text', 'request_id', 'images'}:
        raise ContextError('Invalid command.')
    action = body.get('action')
    if action == 'new':
        request_id = body.get('request_id')
        if set(body) != {'action', 'request_id'} or not isinstance(request_id, str) or not re.fullmatch(r'[a-f0-9-]{36}', request_id):
            raise ContextError('A new conversation requires a request ID.')
        key = ('new', request_id)
        with SEND_LOCK:
            if key not in RECEIPTS:
                if len(RECEIPTS) >= 1000:
                    RECEIPTS.pop(next(iter(RECEIPTS)))
                RECEIPTS[key] = {'accepted': False, 'uncertain': True, 'message': 'Creation outcome unknown. Refresh before trying again.'}
                RECEIPTS[key] = new_thread()
            return RECEIPTS[key]
    images=screenshot_inputs(body.get('images',[]))
    row = thread(body.get('thread_id'))
    if action in ('open','connect'):
        status=request_connection(row['id'],retry=action=='open')
        return {'accepted':True,'status':status,'message':'Connecting this conversation. Queued messages remain saved.' if status!='needs_attention' else 'Conversation connection needs attention. Retry connection.'}
    text = body.get('text','')
    if images and isinstance(text,str) and not text.strip():
        text='Please review the attached screenshots.'
    request_id = body.get('request_id')
    if action not in ('send', 'steer') or not isinstance(text, str) or not 1 <= len(text.strip()) <= 16000 or not isinstance(request_id, str) or not re.fullmatch(r'[a-f0-9-]{36}', request_id):
        raise ContextError('Choose Send or Steer and enter 1–16000 characters.')
    key = (row['id'], request_id)
    with SEND_LOCK:
        if key in RECEIPTS:
            return RECEIPTS[key]
        if len(RECEIPTS) >= 1000:
            RECEIPTS.pop(next(iter(RECEIPTS)))
        dispatched = False
        try:
            with IPC() as client:
                owner = client.owner(row['id'])
                state = history(row)
                if action == 'send' and state['activity'] != 'idle':
                    raise ContextError('Thread is running or its state is unknown. Refresh and use Steer for a running turn.')
                if action == 'steer' and state['activity'] != 'running':
                    raise ContextError('No running turn was observed. Refresh before sending.')
                inputs = [{'type': 'text', 'text': text, 'text_elements': []},*images]
                params = {'conversationId': row['id']}
                if action == 'send':
                    params['turnStart'] = {'request': {'threadId': row['id'], 'input': inputs, 'clientUserMessageId': request_id, **full_access_turn_settings(row['id'])}, 'context': {'inheritThreadSettings': True}}
                    method, version = 'thread-follower-start-turn', 2
                else:
                    params.update(input=inputs, clientUserMessageId=request_id, attachments=[],
                                  restoreMessage={'id': request_id, 'text': text, 'context': {}}, serviceTier=None)
                    method, version = 'thread-follower-steer-turn', 1
                # Once dispatched, never automatically retry an ambiguous outcome.
                RECEIPTS[key] = {'accepted': False, 'uncertain': True, 'message': 'Delivery outcome unknown. Check the conversation before sending again.'}
                dispatched = True
                reply = client.request(method, params, version, owner)
                RECEIPTS[key] = {'accepted': True, 'message': 'Instruction delivered to the local VS Code thread.', 'result': reply.get('result')}
                return RECEIPTS[key]
        except (ContextError,OSError) as error:
            if not dispatched:raise NotDispatched('Conversation is not ready. Your message was not sent.') from error
            raise



def rest(method, path, query, body):
    try:
        if path.endswith(('/codex/queue','/codex/queue/cancel')):
            from message_queue import manager
            queue=manager()
            if method=='GET' and path.endswith('/queue'):return 200,queue.view(query.get('thread_id',[''])[0])
            if method=='POST' and path.endswith('/cancel'):return 200,queue.cancel(body)
            if method=='POST':return 202,queue.submit(body)
            raise ContextError('Unsupported queue operation')
        if method == 'GET' and path.endswith('/codex/threads'):
            offset = int(query.get('offset', ['0'])[0])
            if offset < 0 or offset > 100000:
                raise ContextError('Invalid history offset.')
            return 200, listing(offset)
        if method == 'GET' and '/codex/threads/' in path:
            return 200, detail(path.rsplit('/', 1)[1])
        if method == 'POST' and path.endswith('/codex/commands'):
            return 202, mutate(body)
        raise ContextError('Unknown Codex route.')
    except (ContextError, OSError, sqlite3.Error, ValueError, subprocess.SubprocessError) as error:
        return 409, {'error': {'code': 'codex_unavailable', 'message': str(error)}}
