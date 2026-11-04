"""Local shard RPC for the shared GPU mapper; no Vulkan context in clients.

Linux Unix sockets carry open file descriptors, so containers need only the
socket directory in common; inputs/outputs need not have matching path names.
"""
from array import array
from contextlib import ExitStack
import json
import os
from pathlib import Path
import socket
import struct

MAX_MESSAGE = 65536


def send_message(sock, value, descriptors=()):
    data = json.dumps(value, separators=(',', ':')).encode()+b'\n'
    if len(data) > MAX_MESSAGE:
        raise ValueError('GPU worker message too large')
    ancillary = [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array('i', descriptors))] if descriptors else []
    sent = sock.sendmsg([data], ancillary)
    if sent < len(data):
        sock.sendall(data[sent:])


def receive_message(sock, with_descriptors=False):
    descriptors = []
    try:
        data, controls, flags, _ = sock.recvmsg(MAX_MESSAGE, socket.CMSG_SPACE(3*array('i').itemsize),
                                               getattr(socket, 'MSG_CMSG_CLOEXEC', 0))
        for level, kind, raw in controls:
            if level == socket.SOL_SOCKET and kind == socket.SCM_RIGHTS:
                values = array('i')
                values.frombytes(raw[:len(raw)//values.itemsize*values.itemsize])
                descriptors.extend(values)
        if flags & (socket.MSG_TRUNC | socket.MSG_CTRUNC):
            raise ValueError('Truncated GPU worker message/descriptors')
        while not data.endswith(b'\n'):
            if len(data) >= MAX_MESSAGE:
                raise ValueError('GPU worker message too large')
            more = sock.recv(MAX_MESSAGE-len(data))
            if not more:
                raise ConnectionError('GPU worker disconnected before completion; request was not retried')
            data += more
        if len(data) > MAX_MESSAGE or b'\n' in data[:-1]:
            raise ValueError('Invalid GPU worker framing')
        value = json.loads(data)
        if not isinstance(value, dict):
            raise ValueError('GPU worker message must be an object')
        if not with_descriptors and descriptors:
            raise ValueError('Unexpected GPU worker descriptors')
        result = value, descriptors
        descriptors = []
        return result if with_descriptors else value
    finally:
        for fd in descriptors:
            os.close(fd)


def peer_uid(sock):
    return struct.unpack('3i', sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))[1]


def _connect(path, timeout):
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.settimeout(timeout)
        sock.connect(str(path))
        if peer_uid(sock) != os.getuid():
            raise PermissionError('GPU worker must run as the calling user')
        return sock
    except BaseException:
        sock.close()
        raise


def status(path, timeout=10):
    with _connect(path, timeout) as sock:
        send_message(sock, {'version': 1, 'action': 'status'})
        response = receive_message(sock)
    if not response.get('ok'):
        raise RuntimeError(response.get('error', 'GPU worker rejected status request'))
    return response['result']


def map_reads(path, read1, read2, output, *, expected_index_key, read_batch=256,
              banded=True, diagonal_trace=False, require_fscache=False, timeout=3600):
    if not isinstance(expected_index_key, str) or len(expected_index_key) != 64 or any(c not in '0123456789abcdef' for c in expected_index_key):
        raise ValueError('Shared GPU mapping requires a pinned artifact key')
    if type(read_batch) is not int or not 1 <= read_batch <= 256:
        raise ValueError('Read batch must be 1..256')
    output = Path(output)
    partial = output.with_name(output.name+'.partial')
    if output.exists() or partial.exists():
        raise FileExistsError(output if output.exists() else partial)
    created = False
    try:
        with ExitStack() as stack:
            descriptors = [stack.enter_context(open(read1, 'rb')).fileno()]
            if read2 is not None:
                descriptors.append(stack.enter_context(open(read2, 'rb')).fileno())
            fd = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            created = True
            stack.callback(os.close, fd)
            descriptors.append(fd)
            sock = stack.enter_context(_connect(path, timeout))
            send_message(sock, {'version': 1, 'action': 'map', 'key': expected_index_key,
                               'paired': read2 is not None, 'read_batch': read_batch,
                               'banded': bool(banded), 'diagonal_trace': bool(diagonal_trace),
                               'require_fscache': bool(require_fscache)}, descriptors)
            response = receive_message(sock)
            if not response.get('ok'):
                raise RuntimeError(response.get('error', 'GPU worker mapping failed'))
            result = response['result']
            if result.get('gpu_index_key') != expected_index_key:
                raise ValueError('GPU worker returned a different artifact identity')
            os.fsync(fd)
        partial.replace(output)
        return result
    finally:
        if created:
            partial.unlink(missing_ok=True)


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--socket', required=True, type=Path)
    args = parser.parse_args()
    print(json.dumps(status(args.socket), indent=2))
