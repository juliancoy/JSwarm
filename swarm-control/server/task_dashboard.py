"""Task observations and fixed selected-node controls; no scheduler restart required."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import time
from urllib.parse import urlsplit, parse_qs
from task_events import EventStore
from task_progress import snapshot
from observation_feed import ObservationFeed, SnapshotWarming

class TaskFeed(ObservationFeed):
    def __init__(self):
        super().__init__(lambda: snapshot(), stable_revision=True)

feed=TaskFeed()


def pipeline_snapshot():
    from pipeline_nodes import snapshot
    return snapshot()


pipeline_feed=ObservationFeed(pipeline_snapshot)


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        if self.path!='/api/tasks/machine/control':
            self.send_error(404);return
        origin=urlsplit(self.headers.get('Origin',''))
        host=self.headers.get('Host')
        same_origin=origin.netloc==host or host in ('127.0.0.1:8792','localhost:8792') and origin.netloc=='map.local'
        if (origin.scheme not in ('http','https') or not same_origin
                or origin.path or origin.query or origin.fragment
                or self.headers.get('X-JSwarm-Control')!='1'):
            self.send_error(403,'Same-origin machine control required');return
        try:
            if self.headers.get('Transfer-Encoding') or len(self.headers.get_all('Content-Length',[]))!=1:
                raise ValueError('Expected a bounded JSON request')
            size=int(self.headers['Content-Length'])
            if not 0<size<=1024 or self.headers.get('Content-Type','').split(';')[0]!='application/json':
                raise ValueError('Expected a bounded JSON request')
            self.connection.settimeout(5)
            body=json.loads(self.rfile.read(size))
            if not isinstance(body,dict) or set(body)!={'host','action'} or not all(isinstance(v,str) for v in body.values()):
                raise ValueError('Expected host and action')
            from machine_controls import request
            data=request(body['host'],body['action']);code=202
        except (ValueError,OSError):
            data={'error':'Invalid machine control request'};code=400
        except RuntimeError:
            data={'error':'A command is already in progress for this machine'};code=409
        raw=json.dumps(data).encode()
        self.send_response(code);self.send_header('Content-Type','application/json')
        self.send_header('Cache-Control','no-store');self.send_header('Content-Length',str(len(raw)))
        self.end_headers();self.wfile.write(raw)

    def do_GET(self):
        path=urlsplit(self.path)
        if path.path=='/api/tasks/genome-datasets':
            try:
                from genome_catalog import snapshot as catalog_snapshot
                data=catalog_snapshot();code=200
            except Exception:
                data={'schema':'genome-run-catalog.v1','available':False,'datasets':[],'error':'Run catalog is temporarily unavailable'};code=503
            body=json.dumps(data,allow_nan=False).encode()
            self.send_response(code);self.send_header('Content-Type','application/json')
            self.send_header('Cache-Control','no-store');self.send_header('Content-Length',str(len(body)))
            self.end_headers();self.wfile.write(body);return
        if path.path=='/api/tasks/stream':
            self.stream();return
        if path.path not in ('/api/tasks', '/api/tasks/events', '/api/tasks/pipeline', '/api/tasks/machine', '/api/tasks/logs'):
            self.send_error(404); return
        try:
            if path.path in ('/api/tasks', '/api/tasks/pipeline'):
                source = pipeline_feed if path.path.endswith('/pipeline') else feed
                body, compressed, observed, failed = source.response(self.headers.get('Accept-Encoding', ''))
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Cache-Control', 'no-store')
                self.send_header('Vary', 'Accept-Encoding')
                if compressed:self.send_header('Content-Encoding', 'gzip')
                if observed is not None:self.send_header('X-Observation-Age', str(max(0, round(time.time()-observed, 3))))
                if failed:self.send_header('Warning', '199 - "Observation refresh failed; retained snapshot"')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers();self.wfile.write(body)
                return
            if path.path == '/api/tasks/logs':
                from shard_logs import tail
                try:data=tail(parse_qs(path.query).get('id',[''])[0],snapshot=lambda: pipeline_feed.sample or {'items':[]})
                except ValueError:
                    self.send_error(404,'Unknown shard');return
            elif path.path == '/api/tasks/machine':
                from task_machines import snapshot as machine_snapshot
                query=parse_qs(path.query)
                try:data=machine_snapshot(query.get('host',[''])[0])
                except ValueError:
                    self.send_error(404,'Unknown enrolled machine');return
            elif path.path == '/api/tasks/events':
                query=parse_qs(path.query)
                data=EventStore().history(query.get('run',[None])[0],query.get('after',[0])[0],100)
                # Operational event index only; full payloads remain owner-only in SQLite.
                for event in data['events']:
                    value=event.pop('payload',{})
                    event['status']=value.get('status') if isinstance(value.get('status'),str) else None
                    event['step']=value.get('suite') if isinstance(value.get('suite'),str) else None
            body = json.dumps(data).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers(); self.wfile.write(body)
        except SnapshotWarming:
            self.send_response(503);self.send_header('Retry-After', '1');self.end_headers()
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            self.send_error(503, 'Task measurements unavailable')

    def stream(self):
        try:feed.get()
        except SnapshotWarming:
            self.send_response(503);self.send_header('Retry-After','1');self.end_headers();return
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
    feed.refresh();pipeline_feed.refresh()
    import threading
    from task_event_collector import run
    threading.Thread(target=run,daemon=True).start()
    ThreadingHTTPServer(('127.0.0.1', 8792), Handler).serve_forever()
