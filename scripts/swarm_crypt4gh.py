"""Bounded adapters around upstream Crypt4GH; no custom cipher or key format."""
import hashlib
import io
import logging
import os
from pathlib import Path
import stat


def crypto():
    # Upstream debug logging includes session keys. Never enable it here.
    for name in ('crypt4gh', 'crypt4gh.lib', 'crypt4gh.header', 'crypt4gh.sodium'):
        logger = logging.getLogger(name)
        logger.disabled = True
    from crypt4gh import header, lib
    return header, lib


def public_key(private):
    from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
    return X25519PrivateKey.from_private_bytes(private).public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)


def private_key(path):
    path = Path(path)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
            raise ValueError('Custody key must be an owner-only regular file')
        value = stream.read(33)
    if len(value) != 32:
        raise ValueError('Expected raw 32-byte X25519 custody key')
    return value


def file_digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def read_header(stream):
    """Validate bounds before handing an untrusted header to upstream parsing."""
    prefix = stream.read(16)
    if len(prefix) != 16 or prefix[:8] != b'crypt4gh' or int.from_bytes(prefix[8:12], 'little') != 1:
        raise ValueError('Invalid Crypt4GH header')
    count = int.from_bytes(prefix[12:], 'little')
    if not 1 <= count <= 1024:
        raise ValueError('Invalid Crypt4GH packet count')
    result = bytearray(prefix)
    for _ in range(count):
        size = stream.read(4)
        if len(size) != 4:
            raise ValueError('Truncated Crypt4GH packet')
        length = int.from_bytes(size, 'little')
        if length < 8 or len(result) + length > 1024**2:
            raise ValueError('Crypt4GH header exceeds limit')
        data = stream.read(length - 4)
        if len(data) != length - 4:
            raise ValueError('Truncated Crypt4GH packet')
        result.extend(size); result.extend(data)
    return bytes(result)


def encrypt(source, destination, recipient):
    if len(recipient) != 32:
        raise ValueError('Invalid recipient public key')
    _, lib = crypto()
    lib.encrypt([(0, os.urandom(32), recipient)], source, destination)


def rewrap(source, custody_private, recipient):
    header, _ = crypto()
    original = read_header(source)
    packets = header.reencrypt(list(header.parse(io.BytesIO(original))),
                               [(0, custody_private, None)],
                               [(0, os.urandom(32), recipient)], trim=True)
    return header.serialize(packets), len(original)


def decrypt(source, destination, recipient_private, replacement_header=None):
    _, lib = crypto()
    original = read_header(source)
    # Upstream decrypt consumes a normal header followed by the unchanged body.
    class Joined(io.RawIOBase):
        def __init__(self): self.prefix = io.BytesIO(replacement_header or original)
        def readable(self): return True
        def read(self, size=-1):
            if size < 0: raise ValueError('Unbounded encrypted reads are forbidden')
            first = self.prefix.read(size)
            return first + source.read(size-len(first))
        def readinto(self, buffer):
            data = self.read(len(buffer)); buffer[:len(data)] = data; return len(data)
    lib.decrypt([(0, recipient_private, None)], Joined(), destination)


def ingest(source, destination, recipient):
    """Publish only a complete encrypted file, never overwrite an existing object."""
    import secrets
    source, destination = Path(source), Path(destination)
    temporary = destination.with_name('.'+destination.name+'.'+secrets.token_hex(8)+'.partial')
    try:
        fd = os.open(temporary, os.O_WRONLY|os.O_CREAT|os.O_EXCL, 0o600)
        with source.open('rb') as incoming, os.fdopen(fd, 'wb') as outgoing:
            before = os.fstat(incoming.fileno())
            encrypt(incoming, outgoing, recipient)
            after = os.fstat(incoming.fileno())
            if (before.st_size,before.st_mtime_ns,before.st_ctime_ns) != (after.st_size,after.st_mtime_ns,after.st_ctime_ns):
                raise ValueError('Input changed during encryption')
            outgoing.flush(); os.fsync(outgoing.fileno())
        os.link(temporary, destination)
        directory = os.open(destination.parent, os.O_RDONLY|os.O_DIRECTORY)
        try: os.fsync(directory)
        finally: os.close(directory)
        return {'path': str(destination.resolve()), 'sha256': file_digest(destination), 'bytes': destination.stat().st_size}
    finally:
        temporary.unlink(missing_ok=True)
