#!/usr/bin/env python3
"""Container-only loopback SDK server alongside the isolated Selenium browser."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import subprocess
import threading
import os
import signal


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path=='/':
            body=b'<!doctype html><title>Distributed WebRTC test</title>';kind='text/html'
        elif self.path=='/peer.js':
            body=Path('/p2p/peer.js').read_bytes();kind='text/javascript'
        else:
            self.send_error(404);return
        self.send_response(200);self.send_header('Content-Type',kind)
        self.send_header('Cache-Control','no-store');self.end_headers();self.wfile.write(body)
    def log_message(self,*args):pass


if __name__=='__main__':
    server=ThreadingHTTPServer(('127.0.0.2',8789),Handler)
    threading.Thread(target=server.serve_forever,daemon=True).start()
    process=subprocess.Popen(['/opt/bin/entry_point.sh'],start_new_session=True)
    def expire(*args):
        if process.poll() is None:os.killpg(process.pid,signal.SIGTERM)
    signal.signal(signal.SIGALRM,expire);signal.alarm(1200)
    try:raise SystemExit(process.wait())
    finally:server.shutdown()
