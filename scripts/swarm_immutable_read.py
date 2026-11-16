"""Bounded immutable range reads over the existing enrolled mTLS data service.

Only the node's existing credentials are used. Object grants are peer-bound;
file identity, ETag, range, length, hostname and exact certificate are checked.
"""
import concurrent.futures
from collections import Counter
import errno
import hashlib
import http.client
import json
from pathlib import Path
import re
import socket
import ssl
import threading

MAX_RANGE=256*1024


class IntegrityError(OSError):
    pass


_RESOLVERS = concurrent.futures.ThreadPoolExecutor(max_workers=4, thread_name_prefix='memory-discovery')
_SERVICE_RESOLVERS = concurrent.futures.ThreadPoolExecutor(max_workers=4, thread_name_prefix='memory-dns-sd')
_DISCOVERY = None
_DISCOVERY_NETWORK = None
_DISCOVERY_LOCK = threading.Lock()


def service_candidates(host, port, timeout):
    """DNS-SD complements NSS; advertisements locate peers, TLS authenticates."""
    global _DISCOVERY, _DISCOVERY_NETWORK
    from zeroconf import IPVersion, Zeroconf
    from scripts.launcher_discovery import network_signature, service_hostname
    network = (network_signature(), Path('/proc/net/if_inet6').read_text())
    with _DISCOVERY_LOCK:
        if _DISCOVERY is not None and network != _DISCOVERY_NETWORK:
            _DISCOVERY.close(); _DISCOVERY = None
        if _DISCOVERY is None:
            import atexit
            _DISCOVERY = Zeroconf(unicast=True, ip_version=IPVersion.All)
            _DISCOVERY_NETWORK = network
            atexit.register(_DISCOVERY.close)
        info = _DISCOVERY.get_service_info('_lifetech._tcp.local.', host+'._lifetech._tcp.local.', timeout=int(timeout*1000))
    if (not info or info.port != 8790 or info.server.rstrip('.') not in (host, service_hostname(host))
            or info.properties.get(b'node') != host.encode()
            or info.properties.get(b'protocol') != b'lifetech-launcher.v1'):
        return []
    import ipaddress
    result = []
    for address in info.parsed_scoped_addresses(IPVersion.All):
        ip = ipaddress.ip_address(address.split('%')[0])
        if ip.version == 6 and ip.is_link_local and '%' not in address: continue
        result.extend(socket.getaddrinfo(address, port, socket.AF_UNSPEC, socket.SOCK_STREAM, 0, socket.AI_NUMERICHOST))
    return result


def connect(host,port,timeout=3):
    # Race independent host mDNS/NSS and DNS-SD resolution. Slow or unavailable
    # NSS must not prevent a reachable enrolled service from supplying routes.
    winner=concurrent.futures.Future();guard=threading.Lock();stopped=False
    def attempt(item):
        family,kind,protocol,_,address=item;raw=None
        try:
            try:raw=socket.socket(family,kind,getattr(socket,'IPPROTO_MPTCP',262))
            except OSError as error:
                if error.errno not in (errno.EPROTONOSUPPORT,errno.ENOPROTOOPT,errno.EINVAL,errno.EAFNOSUPPORT,errno.EPERM):raise
                raw=socket.socket(family,kind,protocol)
            raw.settimeout(timeout);raw.setsockopt(socket.IPPROTO_TCP,socket.TCP_NODELAY,1);raw.connect(address)
            with guard:
                if not stopped and not winner.done():winner.set_result(raw);raw=None
        except OSError:pass
        finally:
            if raw is not None:raw.close()
    pool=concurrent.futures.ThreadPoolExecutor(max_workers=8)
    seen=set()
    def resolved(future):
        try: rows=future.result()
        except Exception:return
        ipv6=[r for r in rows if r[0]==socket.AF_INET6]
        ipv4=[r for r in rows if r[0]==socket.AF_INET]
        with guard:
            if stopped:return
            for i in range(max(len(ipv6),len(ipv4))):
                for family in (ipv6,ipv4):
                    if i<len(family) and family[i] not in seen and len(seen)<32:
                        seen.add(family[i]);pool.submit(attempt,family[i])
    resolutions=[_RESOLVERS.submit(socket.getaddrinfo,host,port,socket.AF_UNSPEC,socket.SOCK_STREAM),
                 _SERVICE_RESOLVERS.submit(service_candidates,host,port,min(timeout,1))]
    try:
        for future in resolutions:future.add_done_callback(resolved)
        return winner.result(timeout=timeout+1)
    except concurrent.futures.TimeoutError:raise OSError(errno.ETIMEDOUT,'No reachable enrolled bulk address') from None
    finally:
        with guard:stopped=True
        for future in resolutions:future.cancel()
        pool.shutdown(wait=False,cancel_futures=True)


class Reader:
    def __init__(self,definitions,root=None):
        self.definitions=definitions;self.root=Path(root or Path.home()/'.local/state/lifetech-launcher/bulk-transport')
        self.local=threading.local()
        self.stats=Counter()

    def connection(self,definition):
        if not hasattr(self.local,'connections'):self.local.connections={}
        source=definition['source']
        if source in self.local.connections:
            item=self.local.connections[source]
            if item[0].sock is not None:return item
            item[0].close();self.local.connections.pop(source,None)
        config=json.loads((self.root/'config.json').read_text())
        if config['machine']!=hashlib.sha256(Path('/etc/machine-id').read_bytes()).hexdigest():
            raise IntegrityError(errno.EACCES,'Bulk client machine identity changed')
        peer=config['peers'].get(source)
        if not peer or peer['machine']!=definition['machine']:raise IntegrityError(errno.EACCES,'Immutable source machine changed')
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9.-]*\.local',source) or not peer['address'].endswith('.local'):
            raise IntegrityError(errno.EACCES,'Immutable bulk source requires an enrolled mDNS name')
        context=ssl.create_default_context(cafile=str(self.root/'ca.crt'));context.minimum_version=ssl.TLSVersion.TLSv1_3
        context.load_cert_chain(self.root/'identity.crt',self.root/'identity.key')
        connection=http.client.HTTPSConnection(peer['address'],config['port'],context=context,timeout=10)
        connection._create_connection=lambda address,timeout=10,source_address=None,**kwargs:connect(source,address[1],min(timeout,3))
        try:connection.connect()
        except ssl.SSLError as error:raise IntegrityError(errno.EACCES,'Immutable bulk TLS authentication failed') from error
        actual=hashlib.sha256(connection.sock.getpeercert(binary_form=True)).hexdigest()
        if actual!=peer['certificate_sha256']:
            connection.close();raise IntegrityError(errno.EACCES,'Immutable source certificate pin differs')
        self.stats['ipv6_connections' if connection.sock.family==socket.AF_INET6 else 'ipv4_connections']+=1
        item=(connection,config['name']);self.local.connections[source]=item;return item

    def read(self,path,offset,length):
        definition=self.definitions[path]
        if type(offset) is not int or type(length) is not int or offset<0 or not 0<length<=MAX_RANGE or offset+length>definition['bytes']:
            raise IntegrityError(errno.EINVAL,'Immutable range exceeds declared bounds')
        connection,name=self.connection(definition)
        obj=definition['clients'].get(name)
        if not isinstance(obj,str) or not re.fullmatch('[a-f0-9]{64}',obj):raise IntegrityError(errno.EACCES,'No peer-bound immutable object grant')
        try:
            connection.request('GET','/objects/'+obj,headers={'Range':f'bytes={offset}-{offset+length-1}','If-Match':'"'+definition['sha256']+'"'})
            response=connection.getresponse()
            if response.status in (403,404,409,412):raise IntegrityError(errno.ESTALE,'Immutable range capability or source changed')
            expected=f"bytes {offset}-{offset+length-1}/{definition['bytes']}"
            if (response.status!=206 or response.getheader('Content-Range')!=expected
                    or response.getheader('ETag')!='"'+definition['sha256']+'"'
                    or response.getheader('Content-Length')!=str(length)):
                raise IntegrityError(errno.EIO,'Immutable range response identity differs')
            data=response.read(length+1)
            if len(data)!=length:raise IntegrityError(errno.EIO,'Immutable range truncated')
            return data
        except Exception as error:
            connection.close();self.local.connections.pop(definition['source'],None)
            if isinstance(error,http.client.HTTPException):raise OSError(errno.EIO,'Immutable bulk connection interrupted') from error
            raise
