#!/usr/bin/env python3
"""Carry unchanged SSH bytes over MPTCP; never read keys or decrypt SSH."""
import argparse
import os
import socket
import threading

PORT=8792


def pump(source,destination):
    try:
        while data:=source.recv(65536):destination.sendall(data)
        destination.shutdown(socket.SHUT_WR)
    except OSError:
        try:destination.shutdown(socket.SHUT_RDWR)
        except OSError:pass


def bridge(incoming,upstream):
    thread=threading.Thread(target=pump,args=(incoming,upstream),daemon=True);thread.start()
    try:pump(upstream,incoming)
    finally:
        incoming.close();upstream.close();thread.join(timeout=2)


def exact(sock,size):
    data=bytearray()
    while len(data)<size:
        chunk=sock.recv(size-len(data))
        if not chunk:raise OSError('Transport closed')
        data.extend(chunk)
    return bytes(data)


def server_auth(conn,key):
    import secrets,hmac
    nonce=secrets.token_bytes(32);conn.sendall(nonce+hmac.digest(key,b'server'+nonce,'sha256'))
    if not hmac.compare_digest(exact(conn,32),hmac.digest(key,b'client'+nonce,'sha256')):raise OSError('Transport authentication failed')


def client_auth(conn,key):
    import hmac
    nonce=exact(conn,32);signature=exact(conn,32)
    if not hmac.compare_digest(signature,hmac.digest(key,b'server'+nonce,'sha256')):raise OSError('Transport authentication failed')
    conn.sendall(hmac.digest(key,b'client'+nonce,'sha256'))


def ready_peer(pool):
    import time,queue
    deadline=time.monotonic()+15
    while time.monotonic()<deadline:
        peer=pool.get(timeout=max(.01,deadline-time.monotonic()))
        try:
            peer.settimeout(5);peer.sendall(b'G')
            if exact(peer,1)!=b'R':raise OSError('SSH backend unavailable')
            peer.settimeout(None);return peer
        except OSError:peer.close()
    raise OSError('No ready archive transport')


def serve(address,port,allowed,token_file,unix_path):
    import queue,hmac,hashlib,secrets
    from pathlib import Path
    key=Path(token_file).read_bytes();pool=queue.Queue(maxsize=2)
    listener=socket.socket(socket.AF_INET,socket.SOCK_STREAM,socket.IPPROTO_MPTCP)
    listener.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1);listener.bind((address,port));listener.listen(4)
    path=Path(unix_path);path.unlink(missing_ok=True)
    local=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM);local.bind(str(path));path.chmod(0o600);local.listen(4)
    def accept_peer(conn):
        try:
            conn.settimeout(10);server_auth(conn,key)
            conn.settimeout(None);pool.put_nowait(conn)
        except (OSError,queue.Full):conn.close()
    def local_sessions():
        def session(conn):
            try:
                peer=ready_peer(pool);bridge(conn,peer)
            except (OSError,queue.Empty):conn.close()
        while True:
            conn,_=local.accept();threading.Thread(target=session,args=(conn,),daemon=True).start()
    threading.Thread(target=local_sessions,daemon=True).start()
    slots=threading.BoundedSemaphore(8)
    def authenticate(conn):
        try:accept_peer(conn)
        finally:slots.release()
    while True:
        conn,peer=listener.accept()
        if peer[0] not in allowed or not slots.acquire(blocking=False):conn.close();continue
        threading.Thread(target=authenticate,args=(conn,),daemon=True).start()


def reverse(address,port,token_file):
    import hmac,time
    from pathlib import Path
    key=Path(token_file).read_bytes()
    while True:
        sock=None
        try:
            sock=socket.socket(socket.AF_INET,socket.SOCK_STREAM,socket.IPPROTO_MPTCP);sock.settimeout(15);sock.connect((address,port))
            client_auth(sock,key);sock.settimeout(None)
            if exact(sock,1)!=b'G':raise OSError('Invalid transport session')
            upstream=socket.create_connection(('127.0.0.1',22),timeout=15);upstream.settimeout(None);sock.sendall(b'R');bridge(sock,upstream)
        except OSError:
            if sock:sock.close()
        time.sleep(.2)


def connect(unix_path):
    sock=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM);sock.settimeout(25);sock.connect(unix_path);sock.settimeout(None)
    def upload():
        try:
            while data:=os.read(0,65536):sock.sendall(data)
            sock.shutdown(socket.SHUT_WR)
        except OSError:pass
    threading.Thread(target=upload,daemon=True).start()
    try:
        while data:=sock.recv(65536):
            pending=memoryview(data)
            while pending:pending=pending[os.write(1,pending):]
    finally:sock.close()


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('mode',choices=['serve','reverse','connect']);parser.add_argument('--address');parser.add_argument('--port',type=int,default=PORT);parser.add_argument('--allow',action='append',default=[]);parser.add_argument('--token-file');parser.add_argument('--socket');args=parser.parse_args()
    if args.mode=='serve':
        if not args.allow:parser.error('At least one authorized source address is required')
        serve(args.address,args.port,set(args.allow),args.token_file,args.socket)
    elif args.mode=='reverse':reverse(args.address,args.port,args.token_file)
    else:connect(args.socket)
