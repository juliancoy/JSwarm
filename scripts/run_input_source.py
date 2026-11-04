"""Independent, signed, LAN-only mTLS input server for run-manager streams."""
import argparse
import hashlib
import http.server
import importlib.util
import ipaddress
import json
import signal
import threading
import time
from pathlib import Path
from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey



def byte_range(requested,total):
    if not requested:return 0,total-1
    import re
    match=re.fullmatch(r'bytes=(\d+)-(\d+)',requested)
    if not match:raise ValueError('Invalid range')
    start,end=map(int,match.groups())
    if not 0<=start<=end<total or end-start+1>4*1024**2:raise ValueError('Range outside input bounds')
    return start,end


def main():
    p=argparse.ArgumentParser();p.add_argument('--root',type=Path,required=True);p.add_argument('--delegation',type=Path,required=True);p.add_argument('--cache-only',action='store_true');args=p.parse_args()
    spec=importlib.util.spec_from_file_location('input_peer',args.root/'worker.py');w=importlib.util.module_from_spec(spec);spec.loader.exec_module(w)
    peer=w.Peer(args.root)
    authority=w.load(Path.home()/'.config/lifetech-launcher/peers.json')['controller_public_key']
    auth=w.verified(w.load(args.delegation),Ed25519PublicKey.from_public_bytes(bytes.fromhex(authority)))
    role=peer.name in auth.get('cache_peers',{}) if args.cache_only else auth['source']==peer.name
    if (auth['schema']!='venter-stream-offload.v1' or not role
        or auth['source_module_sha256']!=w.digest(Path(__file__)) or not auth['created']<=time.time()<=auth['expires']):
        raise ValueError('Invalid source stream authorization')
    endpoint=auth['cache_peers'][peer.name] if args.cache_only else {'address':auth['source_address'],'port':auth['source_port']}
    address=endpoint['address'];port=endpoint['port']
    if not ipaddress.ip_address(address).is_private or not 1024<=port<=65535:raise ValueError('LAN input endpoint required')
    objects={j['spec']['source_md5']:j['spec'] for j in auth['jobs']}
    allowed=set(auth.get('cache_peers',{}))|{auth['worker'],auth['source']}
    fingerprints={hashlib.sha256(x509.load_pem_x509_certificate(peer.manifest['nodes'][name]['certificate'].encode()).public_bytes(serialization.Encoding.DER)).hexdigest() for name in allowed}
    cache=None
    if args.cache_only:
        from run_input_cache import InputCache,ram_cache
        if auth.get('cache_module_sha256')!=w.digest(Path(__file__).with_name('run_input_cache.py')):raise ValueError('Cache code differs from authorization')
        cache=ram_cache(auth['input_cache']['budget_bytes']) if auth.get('input_cache',{}).get('tier')=='ram' else InputCache(Path.home()/'.cache/swarm-control/inputs')
    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version='HTTP/1.1'
        def log_message(self,*args):pass
        def do_GET(self):
            try:
                if time.time()>auth['expires'] or hashlib.sha256(self.connection.getpeercert(binary_form=True)).hexdigest()not in fingerprints:
                    self.send_error(403);return
                prefix='/cache/' if args.cache_only else '/objects/'
                md5=self.path.removeprefix(prefix)
                if self.path!=prefix+md5 or md5 not in objects:self.send_error(404);return
                path=None
                if not args.cache_only:
                    receipt=auth.get('source_files',{}).get(md5)
                    if receipt:
                        path=Path(receipt['path']);st=path.stat()
                        if any(getattr(st,key)!=receipt[key] for key in ('st_size','st_mtime_ns','st_ctime_ns','st_ino','st_dev')):
                            self.send_error(409);return
                    else:path=peer.object_path(md5)
                if not args.cache_only and (not path or path.stat().st_size!=objects[md5]['bytes']):self.send_error(409);return
                total=objects[md5]['bytes'];requested=self.headers.get('Range')
                try:start,end=byte_range(requested,total)
                except ValueError:self.send_error(416);return
                if args.cache_only:
                    if not requested:self.send_error(416);return
                    try:data=cache.lookup(md5,total,start)
                    except ValueError:self.send_error(416);return
                    if data is None:self.send_error(404);return
                    if len(data)!=end-start+1:self.send_error(416);return
                self.send_response(206 if requested else 200)
                self.send_header('Accept-Ranges','bytes')
                if requested:self.send_header('Content-Range',f'bytes {start}-{end}/{total}')
                self.send_header('Content-Length',str(end-start+1));self.end_headers()
                if args.cache_only:self.wfile.write(data);return
                with path.open('rb') as source:
                    buffer=bytearray(1024**2);view=memoryview(buffer)
                    source.seek(start);remaining=end-start+1
                    while remaining:
                        n=source.readinto(view[:min(len(buffer),remaining)])
                        if not n:break
                        self.wfile.write(view[:n]);remaining-=n
            except (BrokenPipeError,ConnectionResetError):pass
    class Server(http.server.ThreadingHTTPServer):daemon_threads=True
    server=Server((address,port),Handler);server.socket=peer.context(server=True).wrap_socket(server.socket,server_side=True)
    for sig in (signal.SIGTERM,signal.SIGINT):signal.signal(sig,lambda *_:threading.Thread(target=server.shutdown,daemon=True).start())
    server.serve_forever();server.server_close()

if __name__=='__main__':main()
