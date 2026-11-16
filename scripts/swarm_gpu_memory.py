"""Opt-in immutable GPU owner with imported shared-memory payload buffers.

One owner accepts concurrent transactions into separate bounded GPU I/O slots. Not yet connected
 to minimap2's index lookup or alignment kernels. No network listener.
"""
import argparse
import array
import mmap
import ctypes as C
from concurrent.futures import ThreadPoolExecutor
import queue
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import struct
import threading

MAX_QUERIES = 65536


def receive(sock, view):
    while view:
        n = sock.recv_into(view)
        if not n:
            raise EOFError('truncated request')
        view = view[n:]


def header(sock, value=None):
    if value is not None:
        data = json.dumps(value).encode()
        sock.sendall(struct.pack('!I', len(data)) + data)
        return
    size = bytearray(4)
    receive(sock, memoryview(size))
    length, = struct.unpack('!I', size)
    if length > 4096:
        raise ValueError('oversized header')
    data = bytearray(length)
    receive(sock, memoryview(data))
    return json.loads(data)


def send_fd(sock, fd):
    if sock.sendmsg([b'F'],[(socket.SOL_SOCKET,socket.SCM_RIGHTS,array.array('i',[fd]))]) != 1:
        raise OSError('descriptor handoff failed')


def receive_fd(sock):
    marker,ancillary,flags,_ = sock.recvmsg(1,socket.CMSG_SPACE(4),socket.MSG_CMSG_CLOEXEC)
    descriptors=[]
    for level,kind,data in ancillary:
        if level==socket.SOL_SOCKET and kind==socket.SCM_RIGHTS:
            values=array.array('i'); values.frombytes(data[:len(data)//values.itemsize*values.itemsize])
            descriptors.extend(values)
    if marker != b'F' or flags & (socket.MSG_TRUNC|socket.MSG_CTRUNC) or len(descriptors)!=1:
        for fd in descriptors: os.close(fd)
        raise ValueError('invalid shared descriptor handoff')
    return descriptors[0]


class Session:
    """Single-caller persistent session. Write inputs directly; gather then read outputs.

    inputs/outputs are borrowed uint32 views, valid until close. Release derived
    memoryviews before closing. Shared mode has no socket payload or CPU payload
    memcpy; socket mode provides an explicit compatibility/benchmark baseline.
    """
    def __init__(self,path,digest,transport='shared',control='native'):
        if transport not in ('shared','socket'):
            raise ValueError('invalid transport')
        if control not in ('json','native'):raise ValueError('invalid control protocol')
        self.control=control if transport=='shared' else 'json'
        self.transport=transport;self._fd=-1;self._map=None;self._raw=None
        self.inputs=None;self.outputs=None;self.socket=socket.socket(socket.AF_UNIX)
        try:
            self.socket.settimeout(35);self.socket.connect(str(path))
            _,uid,_=struct.unpack('3i',self.socket.getsockopt(socket.SOL_SOCKET,socket.SO_PEERCRED,12))
            if uid!=os.getuid():raise ValueError('owner UID differs')
            header(self.socket,{'op':'open','transport':transport,'sha256':digest,'control':self.control})
            self.metadata=header(self.socket)
            if 'error' in self.metadata:raise RuntimeError(self.metadata['error'])
            if self.metadata.get('transport')!=transport:raise ValueError('transport differs')
            if self.metadata.get('control','json')!=self.control:raise ValueError('control protocol differs')
            if transport=='shared':
                self._fd=receive_fd(self.socket)
                size=self.metadata.get('mapping_bytes')
                if type(size) is not int or not MAX_QUERIES*8<=size<=1024*1024 or os.fstat(self._fd).st_size!=size:
                    raise ValueError('invalid shared mapping size')
                required=fcntl.F_SEAL_GROW|fcntl.F_SEAL_SHRINK|fcntl.F_SEAL_SEAL
                if fcntl.fcntl(self._fd,fcntl.F_GET_SEALS)&required!=required:
                    raise ValueError('unsealed shared mapping')
                self._map=mmap.mmap(self._fd,size,flags=mmap.MAP_SHARED,prot=mmap.PROT_READ|mmap.PROT_WRITE)
                self._raw=memoryview(self._map)[:MAX_QUERIES*8]
            else:
                self._raw=memoryview(bytearray(MAX_QUERIES*8))
            self.inputs=self._raw[:MAX_QUERIES*4].cast('I')
            self.outputs=self._raw[MAX_QUERIES*4:].cast('I')
        except BaseException:
            self.close();raise

    def gather(self,count):
        if type(count) is not int or not 1<=count<=MAX_QUERIES:
            raise ValueError('invalid query count')
        if self.control=='native':
            self.socket.sendall(struct.pack('<I',count))
            status=bytearray(4);receive(self.socket,memoryview(status))
            code,=struct.unpack('<I',status)
            if code:raise RuntimeError({1:'invalid query count',2:'query index exceeds immutable object',3:'GPU owner failed; restart required'}.get(code,'invalid native completion'))
            return
        header(self.socket,{'op':'gather','count':count})
        if self.transport=='socket':self.socket.sendall(self._raw[:count*4])
        result=header(self.socket)
        if 'error' in result:raise RuntimeError(result['error'])
        if result!={'ok':True}:raise ValueError('invalid completion')
        if self.transport=='socket':receive(self.socket,self._raw[MAX_QUERIES*4:MAX_QUERIES*4+count*4])

    def close(self):
        if self.socket is not None:
            self.socket.close();self.socket=None
        for name in ('inputs','outputs','_raw'):
            view=getattr(self,name,None)
            if view is not None:view.release();setattr(self,name,None)
        try:
            if self._map is not None:self._map.close();self._map=None
        finally:
            if self._fd>=0:os.close(self._fd);self._fd=-1

    def __enter__(self):return self
    def __exit__(self,*ignored):self.close()


def request(path, digest, indices=None, transport='shared'):
    # Compatibility convenience API: creating/returning Python lists still copies.
    # Producers/consumers use Session's views directly to avoid those copies.
    if indices is not None:
        if not 1<=len(indices)<=MAX_QUERIES:raise ValueError('invalid query count')
        with Session(path,digest,transport) as session:
            for i,value in enumerate(indices):session.inputs[i]=value
            session.gather(len(indices))
            return session.outputs[:len(indices)].tolist()
    with socket.socket(socket.AF_UNIX) as sock:
        sock.settimeout(35);sock.connect(str(path))
        header(sock,{'sha256':digest,'count':0})
        accepted=header(sock)
        if 'error' in accepted:raise RuntimeError(accepted['error'])
        result=header(sock)
        if 'error' in result:raise RuntimeError(result['error'])
        return result


class Native:
    def __init__(self, library, shader, device, budget, segment):
        self.lib = C.CDLL(str(library))
        signatures = {
            'gm_open': ([C.c_char_p,C.c_char_p,C.c_uint64,C.c_uint64], C.c_void_p),
            'gm_load': ([C.c_void_p,C.c_char_p,C.c_char_p], C.c_int),
            'gm_gather': ([C.c_void_p,C.c_uint32], C.c_int),
            'gm_slot_transport': ([C.c_void_p,C.c_uint32,C.c_int], C.c_int),
            'gm_slot_fd': ([C.c_void_p,C.c_uint32], C.c_int),
            'gm_slot_mapping_bytes': ([C.c_void_p,C.c_uint32], C.c_uint64),
            'gm_optimize': ([C.c_void_p,C.c_int,C.c_int], C.c_int),
            'gm_tune': ([C.c_void_p,C.c_uint32,C.c_uint32], C.c_int),
            'gm_serve_shared': ([C.c_void_p,C.c_uint32,C.c_int], C.c_int),
            'gm_profile': ([C.c_void_p], C.c_int),
            'gm_healthy': ([C.c_void_p], C.c_int),
            'gm_configure': ([C.c_void_p,C.c_uint32], C.c_int),
            'gm_slot_gather': ([C.c_void_p,C.c_uint32,C.c_uint32], C.c_int),
            'gm_slot_io': ([C.c_void_p,C.c_uint32], C.c_void_p),
            'gm_io': ([C.c_void_p], C.c_void_p),
            'gm_info': ([C.c_void_p], C.c_char_p),
            'gm_error': ([], C.c_char_p), 'gm_close': ([C.c_void_p], None)}
        for name,(args,result) in signatures.items():
            fn = getattr(self.lib,name); fn.argtypes=args; fn.restype=result
        self.ptr = self.lib.gm_open(os.fsencode(shader),device.encode(),budget,segment)
        self.check(self.ptr)

    def check(self, result):
        if not result:
            raise RuntimeError(self.lib.gm_error().decode())

    def info(self):
        return dict(json.loads(self.lib.gm_info(self.ptr)), owner_pid=os.getpid())

    def close(self):
        if self.ptr:
            self.lib.gm_close(self.ptr)
            self.ptr = None


def serve(args):
    if not 1<=args.parallelism<=args.max_sessions<=16:
        raise ValueError('require 1 <= parallelism <= max_sessions <= 16')
    if not re.fullmatch('[0-9a-f]{64}',args.sha256):
        raise ValueError('expected lowercase SHA256')
    directory = args.directory.resolve()
    directory.mkdir(mode=0o700,parents=True,exist_ok=True)
    stat = directory.stat()
    if stat.st_uid != os.getuid() or stat.st_mode & 0o077:
        raise ValueError('owner directory must be private (0700) and owned by this UID')
    native = Native(args.library,args.shader,args.device,args.budget,args.segment)
    try:
        key = hashlib.sha256((native.info()['device_uuid']+args.sha256).encode()).hexdigest()[:32]
        # Device/object lock is taken before upload. Clients reuse the socket.
        with (directory/(key+'.lock')).open('a') as lock:
            try:
                fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:
                raise RuntimeError('existing owner; reuse '+str(directory/(key+'.sock')))
            path = directory/(key+'.sock')
            native.check(native.lib.gm_load(native.ptr,os.fsencode(args.source),args.sha256.encode()))
            native.check(native.lib.gm_optimize(native.ptr,args.command_reuse=='on',args.device_io=='staged'))
            native.check(native.lib.gm_configure(native.ptr,args.max_sessions))
            native.check(native.lib.gm_tune(native.ptr,args.parallelism,args.spin_us))
            if args.profile:native.check(native.lib.gm_profile(native.ptr))
            available = queue.Queue(maxsize=args.max_sessions)
            for i in range(args.max_sessions):available.put(i)
            totals={'shared_batches':0,'socket_batches':0,'socket_payload_bytes':0}
            stats_lock=threading.Lock()

            def info():
                with stats_lock:
                    result=dict(native.info(),parallelism=args.parallelism,session_slots=args.max_sessions,**totals)
                    result['shared_batches']+=result['native_shared_batches']
                    return result

            def healthy():
                if not native.lib.gm_healthy(native.ptr):
                    raise RuntimeError('GPU owner failed; restart required')

            def count_of(message):
                if not isinstance(message,dict):raise ValueError('header must be an object')
                count=message.get('count')
                if type(count) is not int or not 0<=count<=MAX_QUERIES:
                    raise ValueError('invalid query count')
                return count

            def handle(connection,slot):
                io=None
                try:
                    with connection:
                        connection.settimeout(5)
                        try:
                            _,uid,_=struct.unpack('3i',connection.getsockopt(socket.SOL_SOCKET,socket.SO_PEERCRED,12))
                            if uid!=os.getuid():raise ValueError('client UID differs')
                            message=header(connection)
                            if not isinstance(message,dict) or message.get('sha256')!=args.sha256:
                                raise ValueError('invalid object identity')
                            healthy()
                            persistent=message.get('op')=='open'
                            control=message.get('control','json')
                            if control not in ('json','native'):raise ValueError('invalid control protocol')
                            transport=message.get('transport') if persistent else 'socket'
                            if transport not in ('shared','socket'):raise ValueError('invalid transport')
                            if not persistent and count_of(message)==0:
                                header(connection,{'accepted':True});header(connection,info());return
                            native.check(native.lib.gm_slot_transport(native.ptr,slot,transport=='shared'))
                            if transport=='socket':
                                io=memoryview((C.c_ubyte*(MAX_QUERIES*8)).from_address(native.lib.gm_slot_io(native.ptr,slot))).cast('B')
                            if persistent:
                                header(connection,{'accepted':True,'transport':transport,'control':control if transport=='shared' else 'json','mapping_bytes':native.lib.gm_slot_mapping_bytes(native.ptr,slot) if transport=='shared' else 0})
                                if transport=='shared':send_fd(connection,native.lib.gm_slot_fd(native.ptr,slot))
                            else:header(connection,{'accepted':True})
                            if persistent and transport=='shared' and control=='native':
                                native.lib.gm_serve_shared(native.ptr,slot,connection.fileno())
                                # Native control errors terminate this binary stream;
                                # never append a JSON error frame to it.
                                return
                            while True:
                                if persistent:
                                    message=header(connection)
                                    if not isinstance(message,dict) or message.get('op')!='gather':raise ValueError('invalid session operation')
                                count=count_of(message)
                                if not count:raise ValueError('empty gather')
                                healthy()
                                if transport=='socket':receive(connection,io[:count*4])
                                native.check(native.lib.gm_slot_gather(native.ptr,slot,count))
                                header(connection,{'ok':True} if persistent else info())
                                if transport=='socket':connection.sendall(io[MAX_QUERIES*4:MAX_QUERIES*4+count*4])
                                with stats_lock:
                                    totals[transport+'_batches']+=1
                                    if transport=='socket':totals['socket_payload_bytes']+=count*8
                                if not persistent:break
                        except (ValueError,RuntimeError,EOFError,OSError) as error:
                            try:header(connection,{'error':str(error)})
                            except OSError:pass
                finally:
                    if io is not None:io.release()
                    # Retained client FDs cannot alias later sessions: transport
                    # setup always replaces imported pages before reassigning them.
                    # Failed GPU contexts never recycle pending mapped regions.
                    if native.lib.gm_healthy(native.ptr):available.put(slot)

            with socket.socket(socket.AF_UNIX) as listener:
                path.unlink(missing_ok=True)
                listener.bind(str(path)); os.chmod(path,0o600); listener.listen(16)
                try:
                    with ThreadPoolExecutor(max_workers=args.max_sessions) as workers:
                        if args.ready:
                            args.ready.write_text(json.dumps(dict(info(),socket=str(path))))
                        while True:
                            slot = available.get()
                            try: connection,_ = listener.accept()
                            except BaseException:
                                available.put(slot)
                                raise
                            workers.submit(handle,connection,slot)
                finally:
                    path.unlink(missing_ok=True)
    finally:
        native.close()


if __name__ == '__main__':
    base = Path(__file__).resolve().parents[1]/'native/gpu-memory-owner'
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--library',type=Path,default=base/'libgpu-memory-owner.so')
    parser.add_argument('--shader',type=Path,default=base/'gather.spv')
    parser.add_argument('--source',required=True,type=Path)
    parser.add_argument('--sha256',required=True)
    parser.add_argument('--directory',required=True,type=Path)
    parser.add_argument('--ready',type=Path)
    parser.add_argument('--device-io',choices=('mapped','staged'),default='mapped')
    parser.add_argument('--command-reuse',choices=('on','off'),default='on')
    parser.add_argument('--spin-us',type=int,choices=range(101),default=0,help='Bounded fence polling before blocking (0 disables)')
    parser.add_argument('--profile',action='store_true',help='Collect native CPU timings and GPU timestamps')
    parser.add_argument('--max-sessions',type=int,choices=range(1,17),default=16)
    parser.add_argument('--parallelism',type=int,choices=range(1,17),default=4)
    parser.add_argument('--device',default='')
    parser.add_argument('--budget',type=int,default=8*1024**3)
    parser.add_argument('--segment',type=int,default=2*1024**3)
    serve(parser.parse_args())
