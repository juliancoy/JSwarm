#!/usr/bin/env python3
"""Bounded bidirectional SSH, TCP, or built-in MPTCP swarm speed test."""
import hashlib
import json
import math
import os
from pathlib import Path
import shlex
import subprocess
import time

MAX_BYTES = 256 * 1024 * 1024
BLOCK = 256 * 1024


def transfer(stream_in, stream_out, mode, size):
    """Stream deterministic, incompressible blocks with receiver verification."""
    block = hashlib.shake_256(b'JSwarm network speed test v1').digest(BLOCK)
    digest = hashlib.sha256()
    remaining = size
    while remaining:
        count = min(BLOCK, remaining)
        data = block[:count] if mode == 'send' else stream_in.read(count)
        if not data:
            raise RuntimeError('Transfer ended before the requested byte count')
        if mode == 'send':
            stream_out.write(data)
        digest.update(data)
        remaining -= len(data)
    if mode == 'send':
        stream_out.flush()
    return digest.hexdigest()


def receiver_main(mode, size):
    import sys
    if mode not in ('send', 'receive') or not 0 < size <= MAX_BYTES:
        raise ValueError('Invalid remote transfer parameters')
    digest = transfer(sys.stdin.buffer, sys.stdout.buffer, mode, size)
    if mode == 'receive':
        print(json.dumps({'bytes': size, 'sha256': digest}), flush=True)


def measure(command, direction, size, timeout, handshake=False):
    """Use a temporary spool for stderr so a failed child cannot block a pipe."""
    import tempfile
    with tempfile.TemporaryFile() as errors:
        start = time.monotonic()
        process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=errors)
        # A timer also bounds blocking stream reads/writes, not only wait().
        import threading
        timer = threading.Timer(timeout, process.kill)
        timer.start()
        try:
            setup = 0.0
            if handshake:
                if process.stdout.readline(64) != b'READY\n':
                    raise RuntimeError('Remote speed test did not become ready')
                setup = time.monotonic() - start
                start = time.monotonic()
            mode = 'send' if direction == 'upload' else 'receive'
            digest = transfer(process.stdout, process.stdin, mode, size)
            process.stdin.close()
            acknowledgement = process.stdout.read(4096)
            status = process.wait(timeout=timeout)
            elapsed = time.monotonic() - start
            if status:
                errors.seek(0)
                raise RuntimeError(errors.read(4096).decode(errors='replace') or f'Transfer exited {status}')
            if direction == 'upload':
                receipt = json.loads(acknowledgement)
                if receipt != {'bytes': size, 'sha256': digest}:
                    raise RuntimeError('Receiver verification failed')
            else:
                expected = hashlib.sha256()
                block = hashlib.shake_256(b'JSwarm network speed test v1').digest(BLOCK)
                for offset in range(0, size, BLOCK):
                    expected.update(block[:min(BLOCK, size-offset)])
                if expected.hexdigest() != digest:
                    raise RuntimeError('Download verification failed')
            return {'bytes': size, 'seconds': round(elapsed, 4),
                    'Mbps': round(size * 8 / elapsed / 1e6, 2), 'setup_seconds': round(setup, 4), 'sha256_verified': True}
        finally:
            timer.cancel()
            if process.poll() is None:
                process.kill()
            process.wait()
            process.stdout.close()
            if not process.stdin.closed:
                process.stdin.close()


def data_socket(multipath=False):
    import socket
    if not multipath:
        return socket.socket()
    factory = globals().get('transport_socket')
    if factory is None:
        from scripts.launcher_registry import transport_socket as factory
    connection = factory((socket.AF_INET, socket.SOCK_STREAM, 0))
    if connection.getsockopt(socket.SOL_SOCKET, socket.SO_PROTOCOL) != 262:
        connection.close()
        raise RuntimeError('MPTCP socket unavailable; refusing to label TCP as MPTCP')
    return connection


def multipath_info(connection):
    import struct
    info = connection.getsockopt(284, 1, 256)  # SOL_MPTCP, MPTCP_INFO
    flags = struct.unpack_from('=I', info, 8)[0]
    # Counts may include pending joins; negotiation alone does not prove aggregation.
    return {'negotiated': bool(flags & 2), 'tcp_fallback': bool(flags & 1),
            'additional_subflows': info[0],
            'subflows_total': info[80] if len(info) > 80 else 1 + info[0]}


def tcp_receiver(address, token, mode, size, multipath=False):
    """One bounded synthetic-data connection; control stays on verified SSH."""
    import socket
    import hmac
    if mode not in ('send', 'receive') or not 0 < size <= MAX_BYTES:
        raise ValueError('Invalid remote transfer parameters')
    with data_socket(multipath) as server:
        server.settimeout(30)
        server.bind((address, 0))
        server.listen(1)
        print(json.dumps({'port': server.getsockname()[1]}), flush=True)
        connection, _ = server.accept()
        with connection:
            connection.settimeout(30)
            with connection.makefile('rwb', buffering=0) as stream:
                if not hmac.compare_digest(stream.readline(128), (token+'\n').encode()):
                    raise ValueError('Invalid test token')
                # Buffered writers guarantee complete writes under backpressure.
            with connection.makefile('rb') as incoming, connection.makefile('wb') as outgoing:
                outgoing.write(b'READY\n'); outgoing.flush()
                digest = transfer(incoming, outgoing, mode, size)
                if mode == 'receive':
                    outgoing.write((digest+'\n').encode()); outgoing.flush()


def measure_tcp(base, source, address, direction, size, timeout, multipath=False):
    import socket
    import secrets
    import tempfile
    import threading
    token = secrets.token_hex(32)
    mode = 'receive' if direction == 'upload' else 'send'
    code = source + f'\ntcp_receiver({address!r}, {token!r}, {mode!r}, {size}, {multipath!r})\n'
    with tempfile.TemporaryFile() as errors:
        setup_start = time.monotonic()
        process = subprocess.Popen(base + ['python3 -c ' + shlex.quote(code)],
                                   stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=errors)
        timer = threading.Timer(timeout, process.kill); timer.start()
        try:
            line = process.stdout.readline(1024)
            if not line:
                errors.seek(0)
                raise RuntimeError(errors.read(4096).decode(errors='replace') or 'TCP helper failed')
            port = json.loads(line)['port']
            with data_socket(multipath) as connection:
                connection.settimeout(timeout)
                connection.connect((address, port))
                with connection.makefile('rb') as incoming, connection.makefile('wb') as outgoing:
                    outgoing.write((token+'\n').encode()); outgoing.flush()
                    if incoming.readline(64) != b'READY\n':
                        raise RuntimeError('TCP helper did not become ready')
                    setup = time.monotonic() - setup_start
                    start = time.monotonic()
                    digest = transfer(incoming, outgoing, 'send' if direction == 'upload' else 'receive', size)
                    if direction == 'upload':
                        if incoming.readline(128).decode().strip() != digest:
                            raise RuntimeError('Receiver verification failed')
                    elapsed = time.monotonic() - start
                    observed = multipath_info(connection) if multipath else None
            if process.wait(timeout=timeout):
                raise RuntimeError('Remote TCP test failed')
            if direction == 'download':
                expected = hashlib.sha256()
                block = hashlib.shake_256(b'JSwarm network speed test v1').digest(BLOCK)
                for offset in range(0, size, BLOCK):
                    expected.update(block[:min(BLOCK, size-offset)])
                if expected.hexdigest() != digest:
                    raise RuntimeError('Download verification failed')
            return {'bytes': size, 'seconds': round(elapsed, 4),
                    'Mbps': round(size*8/elapsed/1e6, 2), 'setup_seconds': round(setup, 4),
                    'sha256_verified': True, **({'mptcp': observed} if multipath else {})}
        finally:
            timer.cancel()
            if process.poll() is None: process.kill()
            process.wait(); process.stdout.close()


def run(peer, size=64*1024*1024, rounds=3, timeout=30, config=None, address=None, transport='ssh'):
    if not 0 < size <= MAX_BYTES or not 1 <= rounds <= 5 or not math.isfinite(timeout) or not 0 < timeout <= 120:
        raise ValueError('Use 1–256 MiB, 1–5 rounds, and a timeout up to 120 seconds')
    if transport not in ('ssh', 'tcp', 'mptcp'):
        raise ValueError('Expected ssh, tcp, or mptcp transport')
    config = Path(config or os.getenv('LIFETECH_PEER_CONFIG', str(Path.home()/'.config/lifetech-launcher/peers.json')))
    settings = json.loads(config.read_text())
    if peer not in settings['peers'] or peer == settings['name']:
        raise ValueError('Select another enrolled peer by its full node name')
    # Stream the helper over verified SSH without installing remote files.
    import socket
    import ipaddress
    destination = str(ipaddress.ip_address(address)) if address else socket.gethostbyname(peer)
    source = Path(__file__).read_text()
    if transport == 'mptcp':
        import inspect
        from scripts.launcher_registry import MultipathSocket, transport_socket
        source += '\nimport socket, errno\n' + inspect.getsource(MultipathSocket) + '\n' + inspect.getsource(transport_socket)
    base = ['ssh', '-T', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=8',
            '-o', 'Compression=no', '-o', 'ControlMaster=no', '-o', 'ControlPath=none',
            '-o', 'HostKeyAlias=' + peer, destination]
    result = {'source': settings['name'], 'peer': peer, 'destination_address': destination,
              'transport': 'SSH, compression disabled, one TCP stream' if transport == 'ssh' else ('MPTCP using swarm transport_socket; SSH control' if transport == 'mptcp' else 'Direct TCP synthetic data; SSH control; one stream'),
              'scope': 'Application throughput with SHA256 verification; setup reported separately; not raw link capacity',
              'timestamp': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()), 'measurements': []}
    for _ in range(rounds):
        for direction in ('upload', 'download'):
            mode = 'receive' if direction == 'upload' else 'send'
            code = source + f'\nprint("READY", flush=True)\nreceiver_main({mode!r}, {size})\n'
            command = base + ['python3 -c ' + shlex.quote(code)]
            row = (measure_tcp(base, source, destination, direction, size, timeout, multipath=transport == 'mptcp') if transport in ('tcp','mptcp')
                   else measure(command, direction, size, timeout, handshake=True))
            row.update({'from': settings['name'] if direction == 'upload' else peer,
                        'to': peer if direction == 'upload' else settings['name']})
            result['measurements'].append(row)
    return result
