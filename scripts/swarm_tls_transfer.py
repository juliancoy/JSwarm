#!/usr/bin/env python3
"""Private allowlisted, resumable mTLS bulk transfer; no directory browsing.

Provision certificates and the owner-only manifest through an enrolled channel.
The HTTP service is independent of the launcher and conversation services.
"""
import argparse
import fcntl
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import ssl
import subprocess
import time


def identity(stat):
    return [stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns]


def byte_range(value, size):
    if value is None:
        return 0, size - 1, False
    match = re.fullmatch(r'bytes=(\d+)-(\d*)', value)
    if not match:
        raise ValueError('Only one explicit byte range is supported')
    start = int(match[1]); end = int(match[2]) if match[2] else size - 1
    if not 0 <= start <= end < size:
        raise ValueError('Range outside object')
    return start, end, True


def make_server(bind, port, manifest, cert, key, ca):
    manifest = Path(manifest)
    objects = {} if manifest.is_dir() else json.loads(manifest.read_text())
    for name, row in objects.items():
        if not re.fullmatch(r'[a-zA-Z0-9_-]+', name):
            raise ValueError('Invalid opaque object identifier')
        if identity(Path(row['path']).stat()) != row['identity']:
            raise ValueError('Allowlisted source changed')

    class Handler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'

        def log_message(self, *args):
            pass  # Do not log private paths, URLs, or certificate identities.

        def do_PUT(self):
            # Output grants are installed through the enrolled owner channel.
            # Workers receive only an opaque, peer-bound, quota-limited grant.
            match=re.fullmatch(r'/inboxes/([a-f0-9]{64})/([A-Za-z0-9][A-Za-z0-9_.-]{0,79})',self.path)
            self.close_connection=True
            if not match or not manifest.is_dir():self.send_error(404);return
            grant_path=manifest.parent/'inboxes'/(match[1]+'.json')
            try:grant=json.loads(grant_path.read_text())
            except (OSError,ValueError):self.send_error(404);return
            peer=hashlib.sha256(self.connection.getpeercert(binary_form=True)).hexdigest()
            if peer not in grant.get('clients',[]) or grant.get('expires',0)<time.time():self.send_error(403);return
            if grant.get('protected') and not time.time()<grant.get('expires',0)<=time.time()+60:self.send_error(403);return
            if self.headers.get('X-Job-Key')!=grant.get('job_key'):self.send_error(403);return
            name=match[2];limit=grant.get('files',{}).get(name)
            length=self.headers.get('Content-Length','');expected=self.headers.get('X-Content-SHA256','')
            if self.headers.get('Transfer-Encoding') or not length.isdecimal() or not re.fullmatch('[a-f0-9]{64}',expected):self.send_error(400);return
            size=int(length)
            if type(limit) is not int or size>limit:self.send_error(413);return
            directory=Path(grant['destination'])
            if not directory.is_dir() or directory.is_symlink():self.send_error(409);return
            temporary=directory/('.'+name+'.'+secrets.token_hex(8)+'.partial')
            try:
                with (directory/'.upload.lock').open('a') as lock:
                    fcntl.flock(lock,fcntl.LOCK_EX)
                    for stale in directory.glob('.'+name+'.*.partial'):stale.unlink(missing_ok=True)
                    target=directory/name
                    if target.exists() or target.is_symlink():
                        if target.is_symlink() or not target.is_file():self.send_error(409);return
                        with target.open('rb') as stream:actual=hashlib.file_digest(stream,'sha256').hexdigest()
                        if target.stat().st_size!=size or actual!=expected:self.send_error(409);return
                        # Consume the retried body before acknowledging it. The
                        # immutable prior object stays authoritative.
                        left=size;h=hashlib.sha256()
                        while left:
                            block=self.rfile.read(min(left,1024*1024))
                            if not block:raise ConnectionError('Output upload truncated')
                            h.update(block);left-=len(block)
                        if h.hexdigest()!=expected:self.send_error(422);return
                        code=200
                    else:
                        occupied=sum((directory/n).stat().st_size for n in grant['files'] if (directory/n).is_file())
                        if occupied+size>grant['max_bytes'] or shutil.disk_usage(directory).free<size+64*1024**2:self.send_error(507);return
                        h=hashlib.sha256();left=size
                        fd=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
                        with os.fdopen(fd,'wb') as stream:
                            while left:
                                block=self.rfile.read(min(left,1024*1024))
                                if not block:raise ConnectionError('Output upload truncated')
                                h.update(block);stream.write(block);left-=len(block)
                            stream.flush();os.fsync(stream.fileno())
                        if h.hexdigest()!=expected:self.send_error(422);return
                        # A concurrent owner-created destination is never replaced.
                        os.link(temporary,target);temporary.unlink()
                        fd=os.open(directory,os.O_RDONLY|os.O_DIRECTORY)
                        try:os.fsync(fd)
                        finally:os.close(fd)
                        code=201
                    self.send_response(code);self.send_header('Content-Length','0');self.send_header('ETag','"'+expected+'"');self.end_headers()
            except (OSError,ConnectionError,ssl.SSLError):
                self.close_connection=True
            finally:temporary.unlink(missing_ok=True)

        def do_GET(self):
            if self.path=='/health':
                data=b'{"capabilities":["expiring-capabilities-v1"]}'
                self.send_response(200);self.send_header('Content-Type','application/json')
                self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data);return
            name = self.path.removeprefix('/objects/')
            row = None
            if self.path.startswith('/objects/') and re.fullmatch(r'[a-zA-Z0-9_-]+', name):
                if manifest.is_dir():
                    entry = manifest / (name + '.json')
                    if entry.is_file():row = json.loads(entry.read_text())
                else:
                    row = json.loads(manifest.read_text()).get(name)
            if row is None:
                self.send_error(404); return
            fingerprint = hashlib.sha256(self.connection.getpeercert(binary_form=True)).hexdigest()
            if (manifest.is_dir() and not row.get('clients')) or row.get('clients') and fingerprint not in row['clients']:
                self.send_error(403); return
            if 'expires' in row and (type(row['expires']) not in (int,float)
                                      or not time.time() < row['expires'] <= time.time()+60):
                self.send_error(403); return
            try:
                with open(row['path'], 'rb') as source:
                    before = identity(os.fstat(source.fileno()))
                    if before != row['identity']:
                        self.send_error(409); return
                    if self.headers.get('If-Match') not in (None, '"' + row['sha256'] + '"'):
                        self.send_error(412); return
                    size = before[2]
                    try:
                        start, end, partial = byte_range(self.headers.get('Range'), size)
                    except ValueError:
                        self.send_response(416)
                        self.send_header('Content-Range', 'bytes */' + str(size))
                        self.send_header('Content-Length', '0'); self.end_headers(); return
                    self.send_response(206 if partial else 200)
                    self.send_header('Content-Length', str(end - start + 1))
                    self.send_header('Content-Type', 'application/octet-stream')
                    self.send_header('ETag', '"' + row['sha256'] + '"')
                    self.send_header('Accept-Ranges', 'bytes')
                    if partial:
                        self.send_header('Content-Range', f'bytes {start}-{end}/{size}')
                    self.end_headers()
                    self.wfile.flush()
                    # SSLSocket handles the secure fallback if SSL_sendfile/kTLS
                    # is not supported by this Python/OpenSSL/kernel combination.
                    self.connection.sendfile(source, offset=start, count=end - start + 1)
                    if identity(os.fstat(source.fileno())) != before:
                        self.close_connection = True
            except (BrokenPipeError, ConnectionResetError, ssl.SSLError):
                self.close_connection = True

    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_3
    context.load_cert_chain(cert, key)
    context.load_verify_locations(ca)
    context.verify_mode = ssl.CERT_REQUIRED
    if hasattr(ssl, 'OP_ENABLE_KTLS'):
        context.options |= ssl.OP_ENABLE_KTLS
    server = ThreadingHTTPServer((bind, port), Handler)
    server.daemon_threads = True
    server.socket = context.wrap_socket(server.socket, server_side=True)
    return server


def fetch(url, destination, expected_sha256, size, ca, cert, key, *, pinned_key=None, metadata=None):
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (destination.parent / '.transfer.lock').open('a') as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return _fetch(url, destination, expected_sha256, size, ca, cert, key, pinned_key=pinned_key, metadata=metadata)


def _fetch(url, destination, expected_sha256, size, ca, cert, key, *, pinned_key=None, metadata=None):
    """Resume an untrusted partial; publish only after full SHA256 validation."""
    if not url.startswith('https://') or not re.fullmatch(r'[0-9a-f]{64}', expected_sha256) or size < 1:
        raise ValueError('Expected HTTPS URL and immutable object receipt')
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    partial_dir = destination.parent / '.rsync-partial'
    partial_dir.mkdir(exist_ok=True, mode=0o700)
    partial = partial_dir / destination.name
    legacy_partial = Path(str(destination) + '.partial')
    if legacy_partial.exists() and not partial.exists():legacy_partial.replace(partial)
    proof = destination.with_name(destination.name + '.verified.json')
    if destination.exists() and proof.exists():
        expected = {'sha256':expected_sha256,'bytes':size,'identity':identity(destination.stat())}
        try:
            if json.loads(proof.read_text()) == expected:
                publish_progress(metadata, 'cached', size, size);return
        except (ValueError, OSError):pass
    if destination.exists():
        candidate = destination
    else:
        partial.touch(mode=0o600, exist_ok=True)
        partial.chmod(0o600)
        if partial.stat().st_size > size:
            raise ValueError('Partial exceeds declared object size')
        if partial.stat().st_size < size:
            command = ['curl', '--silent', '--show-error', '--fail', '--noproxy', '*',
                            '--proto', '=https', '--tlsv1.3', '--cacert', str(ca),
                            '--cert', str(cert), '--key', str(key), '--connect-timeout', '10',
                            '--speed-time', '60', '--speed-limit', '1024',
                            '--header', 'If-Match: "' + expected_sha256 + '"',
                            '--continue-at', '-', '--output', str(partial)]
            if pinned_key:command += ['--pinnedpubkey', pinned_key]
            command.append(url)
            started = time.monotonic(); initial = partial.stat().st_size
            process = subprocess.Popen(command)
            try:
                while process.poll() is None:
                    publish_progress(metadata, 'running', partial.stat().st_size, size, initial, started)
                    time.sleep(1)
                if process.returncode in (22,51,58,60,90):raise ValueError('HTTPS source receipt or certificate rejected')
                if process.returncode:raise subprocess.CalledProcessError(process.returncode, command)
            finally:
                if process.poll() is None:process.terminate();process.wait()

        candidate = partial
    started=time.monotonic();last=started;count=0
    publish_progress(metadata, 'verifying', 0, size, started=started)
    digest = hashlib.sha256()
    with candidate.open('rb') as stream:
        before = identity(os.fstat(stream.fileno()))
        for block in iter(lambda: stream.read(8 * 1024**2), b''):
            digest.update(block);count+=len(block)
            if time.monotonic()-last>=2:
                publish_progress(metadata,'verifying',count,size,started=started);last=time.monotonic()
        publish_progress(metadata,'verifying',count,size,started=started)
        if identity(os.fstat(stream.fileno())) != before:
            raise ValueError('Transfer file changed during verification')
        if before[2] != size or digest.hexdigest() != expected_sha256:
            if candidate == destination:
                candidate.rename(candidate.with_name(candidate.name + '.mismatch-' + str(time.time_ns())))
                return _fetch(url, destination, expected_sha256, size, ca, cert, key, pinned_key=pinned_key, metadata=metadata)
            raise ValueError('Transfer checksum mismatch; partial retained for inspection')
        os.fsync(stream.fileno())
    candidate.chmod(0o600)
    if candidate != destination:
        candidate.replace(destination)
    proof = destination.with_name(destination.name + '.verified.json')
    atomic_json(proof, {'sha256':expected_sha256, 'bytes':size, 'identity':identity(destination.stat())})
    publish_progress(metadata, 'complete', size, size)
    fd = os.open(destination.parent, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_json(path, value):
    path = Path(path)
    temporary = path.with_name(path.name + '.' + str(os.getpid()) + '.tmp')
    with temporary.open('w') as stream:
        json.dump(value, stream)
        stream.flush(); os.fsync(stream.fileno())
    temporary.chmod(0o600); temporary.replace(path)


def publish_progress(metadata, status, count, total, initial=0, started=None):
    if not metadata:return
    elapsed = max(.001, time.monotonic() - started) if started is not None else 0
    path = Path.home()/'.local/state/lifetech-launcher/transfers'/(metadata['id']+'.json')
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    atomic_json(path, {**metadata, 'transport':'mtls-https', 'status':status, 'sampled_at':time.time(),
                      'completed_bytes':count, 'total_bytes':total, 'transferred_bytes':max(0,count-initial),
                      'average_bytes_per_second':max(0,count-initial)/elapsed if elapsed else 0,
                      'elapsed_seconds':elapsed, 'phase': 'SHA256 verification' if status=='verifying' else 'Transfer',
                      'attempt_started':time.time()-elapsed if started is not None else None,
                      'eta_seconds':max(0,total-count)/(max(0,count-initial)/elapsed) if elapsed and count>initial and total>count else None})


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='action', required=True)
    server = sub.add_parser('serve')
    server.add_argument('--bind', required=True)
    server.add_argument('--port', type=int, required=True)
    server.add_argument('--manifest', required=True)
    client = sub.add_parser('fetch')
    client.add_argument('--url', required=True)
    client.add_argument('--destination', required=True)
    client.add_argument('--sha256', required=True)
    client.add_argument('--size', type=int, required=True)
    for command in (server, client):
        for name in ('ca', 'cert', 'key'):
            command.add_argument('--' + name, required=True)
    args = parser.parse_args()
    if args.action == 'serve':
        make_server(args.bind, args.port, args.manifest, args.cert, args.key, args.ca).serve_forever()
    else:
        fetch(args.url, args.destination, args.sha256, args.size, args.ca, args.cert, args.key, metadata={'id':'object-'+hashlib.sha256(str(Path(args.destination)).encode()).hexdigest()[:24]})


if __name__ == '__main__':
    try:
        main()
    except ValueError as error:
        raise SystemExit(78) from error
