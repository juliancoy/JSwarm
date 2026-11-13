"""DNS-SD discovery on the enrolled LAN; advertisements never grant membership."""
import socket
import threading
from zeroconf import IPVersion, ServiceBrowser, ServiceInfo, Zeroconf

SERVICE = '_lifetech._tcp.local.'
_RESOLVER = None
_RESOLVER_LOCK = threading.Lock()


def resolve_service(node, timeout=5000):
    """Resolve DNS-SD directly when a container has no host NSS mDNS module."""
    global _RESOLVER
    with _RESOLVER_LOCK:
        if _RESOLVER is None:
            import atexit
            _RESOLVER = Zeroconf(unicast=True, ip_version=IPVersion.V4Only)
            atexit.register(_RESOLVER.close)
        zc = _RESOLVER
    info = zc.get_service_info(SERVICE, node+'.'+SERVICE, timeout=timeout)
    if (not info or info.port != 8790
            or info.server.rstrip('.') not in (service_hostname(node), node)
            or info.properties.get(b'node') != node.encode()
            or info.properties.get(b'protocol') != b'lifetech-launcher.v1'):
        raise OSError('No enrolled mDNS advertisement for '+node)
    addresses = info.parsed_addresses(IPVersion.V4Only)
    if not addresses:raise OSError('No mDNS addresses for '+node)
    return info.server.rstrip('.'), addresses


def service_hostname(node):
    """Keep swarm A records separate from Avahi's host/interface records."""
    import re
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9-]{0,62}\.local', node):
        raise ValueError('Expected an enrolled .local node name')
    label=node[:-6] + '-swarm'
    if len(label)>63:
        import hashlib
        label=node[:-6][:48]+'-'+hashlib.sha256(node.encode()).hexdigest()[:8]+'-swarm'
    return label + '.local'


def resolve_endpoint(node):
    """Use mDNS names, including legacy nodes during a rolling update."""
    last = None
    for hostname in (service_hostname(node), node):
        try:
            rows = socket.getaddrinfo(hostname, 8790, type=socket.SOCK_STREAM)
            addresses = sorted({row[4][0].split('%', 1)[0] for row in rows})
            if addresses:
                return 'http://' + hostname + ':8790', addresses
        except socket.gaierror as error:
            last = error
    raise OSError('No mDNS endpoint for ' + node) from last


def attest_addresses(host, status):
    resolved = host.get('resolved_addresses') or [host.get('lan_address')]
    matches = set(resolved).intersection(status.get('addresses', []))
    if not matches:
        raise RuntimeError('Resolved agent address is not on an attested physical interface')
    host['lan_address'] = sorted(matches)[0]


class Discovery:
    def __init__(self, name, peers, port, addresses=None):
        self.name, self.peers = name, dict(peers)
        addresses=addresses or [peers[name]]
        self.address = addresses[0]
        self.lock = threading.Lock()
        self.services = {}
        self.zc = Zeroconf(interfaces=addresses, ip_version=IPVersion.V4Only)
        self.info = ServiceInfo(SERVICE, name + '.' + SERVICE,
            addresses=[socket.inet_aton(address) for address in addresses], port=port,
            properties={'node': name, 'protocol': 'lifetech-launcher.v1', 'agent':'lifetech-agent.v1'},
            server=service_hostname(name) + '.',
            host_ttl=45, other_ttl=45)
        self.browser = None

    def start(self):
        self.zc.register_service(self.info)
        self.browser = ServiceBrowser(self.zc, SERVICE, listener=self)

    def add_service(self, zc, kind, name):
        self.update_service(zc, kind, name)

    def update_service(self, zc, kind, name):
        with self.lock:
            self.services.pop(name, None)
        info = zc.get_service_info(kind, name, timeout=2000)
        if not info:
            return
        node = info.properties.get(b'node', b'').decode('utf-8', errors='replace')
        if (node == self.name or node not in self.peers or
                info.properties.get(b'protocol') != b'lifetech-launcher.v1' or info.port != 8790):
            return
        # Enrollment constrains the node and service name, not its DHCP address.
        # The encrypted protocol still authenticates every connection.
        hostname = info.server.rstrip('.')
        if hostname in (service_hostname(node), node) and info.parsed_addresses(IPVersion.V4Only):
            with self.lock:
                self.services[name] = (node, hostname, info.port)

    def remove_service(self, zc, kind, name):
        with self.lock:
            self.services.pop(name, None)

    def endpoint(self, node):
        with self.lock:
            return next(((address, port) for peer, address, port in self.services.values()
                         if peer == node), None)

    def close(self):
        if self.browser:
            self.browser.cancel()
        self.zc.unregister_service(self.info)
        self.zc.close()
