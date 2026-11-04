import hashlib
import socket
import threading
from scripts.ssh_multipath import bridge


def test_bridge_preserves_bytes_and_half_close():
    client,left=socket.socketpair();right,server=socket.socketpair()
    thread=threading.Thread(target=bridge,args=(left,right));thread.start()
    payload=b'encrypted SSH bytes'*1000
    client.sendall(payload);client.shutdown(socket.SHUT_WR)
    received=bytearray()
    while data:=server.recv(65536):received.extend(data)
    assert hashlib.sha256(received).digest()==hashlib.sha256(payload).digest()
    server.sendall(b'checksum verified');server.shutdown(socket.SHUT_WR)
    assert client.recv(1024)==b'checksum verified'
    client.close();server.close();thread.join(timeout=3);assert not thread.is_alive()


def test_transport_mutually_authenticates_and_rejects_a_wrong_key():
    from scripts.ssh_multipath import server_auth,client_auth
    for key,accepted in [(b'k'*32,True),(b'wrong'*8,False)]:
        a,b=socket.socketpair();a.settimeout(2);b.settimeout(2);errors=[]
        def authenticate():
            try:server_auth(a,b'k'*32)
            except OSError as error:errors.append(error)
        thread=threading.Thread(target=authenticate);thread.start()
        try:
            if accepted:client_auth(b,key)
            else:
                import pytest
                with pytest.raises(OSError,match='authentication'):client_auth(b,key)
        finally:b.close();thread.join(timeout=3);a.close()
        assert bool(errors)!=accepted


def test_stale_queued_connection_does_not_break_the_next_ssh_session():
    import queue
    from scripts.ssh_multipath import ready_peer
    pool=queue.Queue();dead,closed=socket.socketpair();closed.close();pool.put(dead)
    live,remote=socket.socketpair();pool.put(live)
    def ready():
        assert remote.recv(1)==b'G';remote.sendall(b'R')
    thread=threading.Thread(target=ready);thread.start()
    assert ready_peer(pool) is live
    thread.join(timeout=2);live.close();remote.close()
