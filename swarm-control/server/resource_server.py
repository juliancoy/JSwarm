#!/usr/bin/env python3
"""Swarm Node resource feed, independently supervised from active job coordination."""
import json
import threading
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from cpu import CpuMonitor
from network_health import NetworkHealth


def worker_request_allowed(headers):
    from urllib.parse import urlsplit
    origin=urlsplit(headers.get('Origin',''))
    return (origin.scheme=='http' and origin.netloc==headers.get('Host') and
            origin.hostname in ('map.local','10.42.0.1','127.0.0.1','localhost') and
            headers.get('Content-Type','').split(';')[0]=='application/json')


def merge_performance(hosts,cache):
    """CPU and peripheral samples must not erase an independently stamped monitor."""
    for host in hosts:
        value=host.get('performance')
        previous=cache.get(host['host'])
        if value and (not previous or value.get('sampled_at',0)>previous.get('sampled_at',0)):
            cache[host['host']]=value
        elif previous:
            host['performance']=previous
    return hosts


class Resources:
    def __init__(self):
        self.monitor=CpuMonitor()
        self.performance={}
        self.network=NetworkHealth()

    def snapshot(self):
        value=self.monitor.view()
        value['hosts']=self.network.merge(merge_performance(value['hosts'],self.performance))
        return value


def main():
    resources=Resources()
    threading.Thread(target=resources.network.worker,daemon=True,name='interface-health').start()
    threading.Thread(target=resources.monitor.worker,daemon=True,name='swarm-resources').start()
    class Handler(BaseHTTPRequestHandler):
        def reply(self, value, code=200):
            body=json.dumps(value,allow_nan=False).encode()
            self.send_response(code);self.send_header('Content-Type','application/json');self.send_header('Cache-Control','no-store')
            self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)

        def do_POST(self):
            if self.path!='/api/worker-controls':self.send_error(404);return
            if not worker_request_allowed(self.headers):
                self.reply({'error':'Use the worker controls on Map.local.'},403);return
            try:
                size=int(self.headers.get('Content-Length','0'))
                if not 0<size<=1024:raise ValueError('Invalid request size')
                value=json.loads(self.rfile.read(size))
                if value.get('action') not in ('pause','stop','resume'):raise ValueError('Unknown action')
                from worker_controls import operate, LOCK
                if not LOCK.acquire(blocking=False):self.reply({'error':'Another worker action is still running.'},409);return
                try:result=operate(value.get('host'),value['action'])
                finally:LOCK.release()
                self.reply(result)
            except ValueError:self.reply({'error':'Invalid worker request.'},400)
            except Exception:self.reply({'error':'Could not verify the change. Refresh status before retrying.'},503)

        def do_GET(self):
            if self.path=='/api/worker-controls':
                from worker_controls import snapshot
                self.reply(snapshot());return
            if self.path.split('?',1)[0] not in ('/api/resources','/api/v1/swarm/task-manager','/api/v1/swarm/connections','/api/v1/swarm/wgs','/health'):
                self.send_error(404);return
            if self.path.split('?',1)[0]=='/api/v1/swarm/task-manager':
                from status import task_manager
                try:value=task_manager()
                except (OSError,ValueError):self.send_error(503);return
            elif self.path=='/api/v1/swarm/connections':
                from status import connections
                value=connections()
            elif self.path=='/api/v1/swarm/wgs':
                from wgs_progress import snapshot
                value=snapshot()
            else:value={'status':'ok','service':'swarm-node-resources'} if self.path=='/health' else resources.snapshot()
            body=json.dumps(value,allow_nan=False).encode()
            self.send_response(200);self.send_header('Content-Type','application/json');self.send_header('Cache-Control','no-store')
            self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
        def log_message(self,*args):pass
    ThreadingHTTPServer(('127.0.0.1',8795),Handler).serve_forever()


if __name__=='__main__':main()
