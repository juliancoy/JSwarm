"""Isolated read-only task dashboard API; no scheduler restart required."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import hashlib
import threading
import time
from urllib.parse import urlsplit, parse_qs
from task_events import EventStore
from task_progress import snapshot

class TaskFeed:
    def __init__(self):
        self.lock=threading.Lock();self.sample=None;self.updated=0;self.revision=0;self.digest=None
    def get(self):
        with self.lock:
            if time.monotonic()-self.updated>=2 or self.sample is None:
                sample=snapshot()
                digest=hashlib.sha256(json.dumps({k:v for k,v in sample.items() if k!='sampled_at'},sort_keys=True).encode()).hexdigest()
                if digest!=self.digest:self.revision+=1;self.digest=digest
                self.sample=sample;self.updated=time.monotonic()
            return self.revision,self.sample

feed=TaskFeed()


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        path=urlsplit(self.path)
        if path.path=='/api/tasks/stream':
            self.stream();return
        if path.path not in ('/api/tasks', '/api/tasks/events', '/api/tasks/pipeline', '/api/tasks/machine'):
            self.send_error(404); return
        try:
            if path.path == '/api/tasks/machine':
                from task_machines import snapshot as machine_snapshot
                query=parse_qs(path.query)
                try:data=machine_snapshot(query.get('host',[''])[0])
                except ValueError:
                    self.send_error(404,'Unknown enrolled machine');return
            elif path.path == '/api/tasks/pipeline':
                from task_pipeline import snapshot as pipeline_snapshot
                data=pipeline_snapshot()
            elif path.path == '/api/tasks/events':
                query=parse_qs(path.query)
                data=EventStore().history(query.get('run',[None])[0],query.get('after',[0])[0],100)
                # Operational event index only; full payloads remain owner-only in SQLite.
                for event in data['events']:
                    value=event.pop('payload',{})
                    event['status']=value.get('status') if isinstance(value.get('status'),str) else None
                    event['step']=value.get('suite') if isinstance(value.get('suite'),str) else None
            else:
                _,data=feed.get()
            body = json.dumps(data).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers(); self.wfile.write(body)
        except Exception:
            self.send_error(503, 'Task measurements unavailable')

    def stream(self):
        self.send_response(200)
        self.send_header('Content-Type','text/event-stream')
        self.send_header('Cache-Control','no-cache, no-transform')
        self.send_header('X-Accel-Buffering','no')
        self.end_headers()
        last=None
        try:
            while True:
                revision,data=feed.get()
                if revision!=last:
                    self.wfile.write(('id: '+str(revision)+'\nevent: snapshot\ndata: '+json.dumps(data)+'\n\n').encode());last=revision
                else:self.wfile.write(b': heartbeat\n\n')
                self.wfile.flush();time.sleep(2)
        except (BrokenPipeError,ConnectionResetError):pass


if __name__ == '__main__':
    import threading
    from task_event_collector import run
    threading.Thread(target=run,daemon=True).start()
    ThreadingHTTPServer(('127.0.0.1', 8792), Handler).serve_forever()
