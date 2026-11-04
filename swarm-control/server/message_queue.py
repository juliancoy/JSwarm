"""Durable owner-authorized FIFO delivery; ambiguous dispatches are never retried."""
from contextlib import closing
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import threading
import time
import codex
from paths import STATE

class MessageQueue:
    def __init__(self,path=None):
        self.path=Path(path or STATE/'messages/queue.sqlite')
        self.path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
        self.lock=threading.Lock();self.worker_lock=threading.Lock()
        with closing(self.db()) as db,db:
            db.execute('CREATE TABLE IF NOT EXISTS messages (seq INTEGER PRIMARY KEY, id TEXT UNIQUE NOT NULL, thread_id TEXT NOT NULL, payload TEXT NOT NULL, digest TEXT NOT NULL, status TEXT NOT NULL, created REAL NOT NULL, updated REAL NOT NULL, note TEXT NOT NULL)')

        os.chmod(self.path,0o600)

    def db(self):
        db=sqlite3.connect(self.path,timeout=10);db.row_factory=sqlite3.Row
        return db

    def submit(self,body):
        if not isinstance(body,dict) or set(body)-{'thread_id','text','images','request_id'}:raise ValueError('Invalid queued message')
        ident=body.get('request_id');text=body.get('text','');thread_id=body.get('thread_id')
        if not isinstance(ident,str) or not re.fullmatch(r'[a-f0-9-]{36}',ident):raise ValueError('A request ID is required')
        if not isinstance(text,str) or len(text)>16000:raise ValueError('Message exceeds 16000 characters')
        images=body.get('images',[]);codex.screenshot_inputs(images)
        if not text.strip() and not images:raise ValueError('Enter a message or attach a screenshot')
        codex.thread(thread_id)
        payload=json.dumps({'action':'send','thread_id':thread_id,'text':text,'images':images,'request_id':ident},sort_keys=True)
        digest=hashlib.sha256(payload.encode()).hexdigest();now=time.time()
        with self.lock,closing(self.db()) as db,db:
            db.execute('BEGIN IMMEDIATE')
            old=db.execute('SELECT digest,status FROM messages WHERE id=?',(ident,)).fetchone()
            if old:
                if old['digest']!=digest:raise ValueError('Request ID already belongs to another message')
                return {'accepted':True,'id':ident,'status':old['status'],'message':'Message already recorded in the queue.'}
            if db.execute("SELECT count(*) FROM messages WHERE status IN ('queued','delivering','uncertain')").fetchone()[0]>=100:raise ValueError('Queue full. Review or cancel pending messages.')
            db.execute("INSERT INTO messages(id,thread_id,payload,digest,status,created,updated,note) VALUES(?,?,?,?,'queued',?,?,?)",(ident,thread_id,payload,digest,now,now,'Waiting for the thread to be idle and connected.'))
            db.execute("DELETE FROM messages WHERE status IN ('delivered','cancelled','reviewed') AND updated<?",(now-7*86400,))
        return {'accepted':True,'id':ident,'status':'queued','message':'Message queued. It will send when this thread is idle and connected.'}

    def view(self,thread_id):
        codex.thread(thread_id)
        with closing(self.db()) as db:
            rows=db.execute("SELECT id,status,created,note,json_extract(payload,'$.text') AS text FROM messages WHERE thread_id=? ORDER BY seq DESC LIMIT 100",(thread_id,)).fetchall()
        return {'messages':[{'id':r['id'],'status':r['status'],'created':r['created'],'note':r['note'],'text':r['text']} for r in rows]}

    def cancel(self,body):
        if not isinstance(body,dict) or set(body)!={'id'}:raise ValueError('Specify a queue ID')
        with self.lock,closing(self.db()) as db,db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT status FROM messages WHERE id=?',(body['id'],)).fetchone()
            if not row or row['status'] not in ('queued','uncertain'):raise ValueError('Only waiting or uncertain messages can be dismissed')
            status='reviewed' if row['status']=='uncertain' else 'cancelled'
            db.execute('UPDATE messages SET status=?,updated=?,note=? WHERE id=?',(status,time.time(),'Dismissed by the owner; no retry will be made.',body['id']))
            db.execute("UPDATE messages SET payload=json_set(payload,'$.images',json('[]')) WHERE id=?",(body['id'],))
        return {'accepted':True}

    def send_now(self,body):
        if not isinstance(body,dict) or set(body)!={'id'} or not isinstance(body['id'],str):raise ValueError('Specify a queue ID')
        with closing(self.db()) as db:
            row=db.execute('SELECT id,thread_id,status,payload FROM messages WHERE id=?',(body['id'],)).fetchone()
        if not row:raise ValueError('Queued message not found')
        if row['status'] in ('delivered','delivering'):
            return {'accepted':row['status']=='delivered','status':row['status']}
        if row['status']!='queued':raise ValueError('Only a waiting message can be sent now')
        state=codex.detail(row['thread_id'])
        if not state.get('owner_connected') or state.get('activity') not in ('idle','running'):
            raise ValueError('Conversation must be connected with a known turn state')
        with self.lock,closing(self.db()) as db,db:
            db.execute('BEGIN IMMEDIATE')
            changed=db.execute("UPDATE messages SET status='delivering',updated=?,note='Sending now to VS Code…' WHERE id=? AND status='queued' AND NOT EXISTS (SELECT 1 FROM messages active WHERE active.thread_id=messages.thread_id AND active.status='delivering')",(time.time(),row['id'])).rowcount
        if not changed:raise ValueError('Message changed or another message is already sending')
        payload=json.loads(row['payload']);payload['action']='steer' if state['activity']=='running' else 'send'
        try:result=codex.mutate(payload);status='delivered' if result.get('accepted') else 'uncertain'
        except codex.NotDispatched:status='queued'
        except Exception:status='uncertain'
        note='Connection changed before sending. Your message is saved and will retry.' if status=='queued' else 'Delivered to the local VS Code thread.' if status=='delivered' else 'Delivery is uncertain. Check the conversation, then mark reviewed. This message will not retry automatically.'
        with self.lock,closing(self.db()) as db,db:
            db.execute('UPDATE messages SET status=?,updated=?,note=? WHERE id=?',(status,time.time(),note,row['id']))
            if status=='delivered':db.execute("UPDATE messages SET payload=json_set(payload,'$.images',json('[]')) WHERE id=?",(row['id'],))
        return {'accepted':status=='delivered','status':status,'message':note}

    def tick(self):
        if not self.worker_lock.acquire(blocking=False):return
        try:
            with closing(self.db()) as db:
                rows=db.execute("SELECT id,thread_id,status FROM messages WHERE status IN ('queued','delivering','uncertain') ORDER BY seq").fetchall()
            seen=set()
            for row in rows:
                if row['thread_id'] in seen:continue
                seen.add(row['thread_id'])
                if row['status']!='queued':continue
                try:state=codex.detail(row['thread_id'])
                except Exception:continue
                if not state.get('owner_connected'):
                    status=state.get('connection_state')
                    if status in ('detached','connecting'):
                        status=codex.request_connection(row['thread_id'])
                    note='Could not attach this conversation. Retry connection; your message is saved.' if status=='needs_attention' else 'Local Codex is unavailable. Your message is saved; waiting for it to reconnect.' if status=='unavailable' else 'Connecting this conversation automatically. Your message is saved.'
                    with closing(self.db()) as db,db:
                        db.execute("UPDATE messages SET note=? WHERE id=? AND status='queued' AND note!=?",(note,row['id'],note))
                    continue
                if state.get('activity')!='idle':continue
                with self.lock,closing(self.db()) as db,db:
                    changed=db.execute("UPDATE messages SET status='delivering',updated=?,note='Sending to VS Code…' WHERE id=? AND status='queued' AND NOT EXISTS (SELECT 1 FROM messages active WHERE active.thread_id=messages.thread_id AND active.status='delivering')",(time.time(),row['id'])).rowcount
                if not changed:continue
                try:
                    with closing(self.db()) as db:payload=json.loads(db.execute('SELECT payload FROM messages WHERE id=?',(row['id'],)).fetchone()[0])
                    result=codex.mutate(payload)
                    status='delivered' if result.get('accepted') else 'uncertain'
                except codex.NotDispatched:status='queued'
                except Exception:status='uncertain'
                note='Connection changed before sending. Your message is saved and will retry.' if status=='queued' else 'Delivered to the local VS Code thread.' if status=='delivered' else 'Delivery is uncertain. Check the conversation, then mark reviewed. This message will not retry automatically.'
                with self.lock,closing(self.db()) as db,db:
                    db.execute('UPDATE messages SET status=?,updated=?,note=? WHERE id=?',(status,time.time(),note,row['id']))
                    if status=='delivered':db.execute("UPDATE messages SET payload=json_set(payload,'$.images',json('[]')) WHERE id=?",(row['id'],))
        finally:self.worker_lock.release()

_QUEUE=None
_START=threading.Lock()
_WORKER_FILE=None
def manager(start_worker=False):
    global _QUEUE,_WORKER_FILE
    with _START:
        if _QUEUE is None:_QUEUE=MessageQueue()
        if start_worker and _WORKER_FILE is None:
            handle=_QUEUE.path.with_suffix('.worker.lock').open('a')
            try:fcntl.flock(handle,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:handle.close();return _QUEUE
            _WORKER_FILE=handle
            with closing(_QUEUE.db()) as db,db:
                db.execute("UPDATE messages SET status='uncertain',note='Service restarted during delivery. Check the conversation before dismissing.' WHERE status='delivering'")
            def work():
                while True:
                    try:_QUEUE.tick()
                    except Exception:pass  # Never log private message contents.
                    time.sleep(2)
            threading.Thread(target=work,name='codex-message-queue',daemon=True).start()
    return _QUEUE
