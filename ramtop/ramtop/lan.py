"""LAN inventory and host-to-neighbor TCP payload accounting, without packet capture."""
from concurrent.futures import ThreadPoolExecutor
import ipaddress
import json
from pathlib import Path
import socket
import shutil
import subprocess
import threading
import time

from .hardware import read
from .io_metrics import NetworkMonitor


def ip_json(*arguments):
    result = subprocess.run(['ip', '-j', *arguments], capture_output=True, text=True, timeout=3)
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or 'ip failed')
    value = json.loads(result.stdout)
    if not isinstance(value, list):
        raise ValueError('ip returned an invalid inventory')
    return value


def interfaces_from_json(items):
    interfaces = {}
    for item in items:
        name = item.get('ifname', '')
        if not name or name == 'lo':
            continue
        addresses, networks = [], []
        for entry in item.get('addr_info', []):
            try:
                interface = ipaddress.ip_interface(f'{entry["local"]}/{entry["prefixlen"]}')
            except (KeyError, ValueError):
                continue
            if interface.ip.is_loopback or interface.ip.is_unspecified:
                continue
            addresses.append(str(interface.ip))
            networks.append(interface.network)
        if addresses:
            interfaces[name] = dict(addresses=addresses, networks=networks,
                                    mac=item.get('address', ''), up=item.get('operstate') == 'UP')
    return interfaces


def endpoint_address(endpoint):
    try:
        address = endpoint.rsplit(':', 1)[0].strip('[]')
        return str(ipaddress.ip_address(address.split('%', 1)[0]))
    except ValueError:
        return None


def hosts_names(text):
    names = {}
    for line in text.splitlines():
        fields = line.split('#', 1)[0].split()
        if len(fields) >= 2:
            try:
                names[str(ipaddress.ip_address(fields[0]))] = fields[1]
            except ValueError:
                pass
    return names


def mdns_nodes(text, interface):
    nodes = {}
    for line in text.splitlines():
        fields = line.split(';')
        if len(fields) >= 8 and fields[0] == '=' and fields[1] == interface:
            try:
                address = str(ipaddress.ip_address(fields[7]))
            except ValueError:
                continue
            nodes[address] = fields[6]
    return nodes


def probe_targets(interface_info, limit=512):
    targets, skipped = set(), []
    for network in interface_info['networks']:
        if network.version != 4:
            continue
        if network.num_addresses > limit + 2 or len(targets) + network.num_addresses > limit:
            skipped.append(str(network))
            continue
        targets.update(str(address) for address in network.hosts() if str(address) not in interface_info['addresses'])
    return sorted(targets, key=ipaddress.ip_address), skipped


def discover_nodes(interface, info):
    """Explicit, bounded discovery: one ICMP probe/address and Avahi service discovery."""
    targets, skipped = probe_targets(info)
    notes, found = [], {}
    if not shutil.which('ping'):
        targets = []
        notes.append('ICMP discovery unavailable: ping is not installed.')
    def probe(address):
        try:
            result = subprocess.run(['ping', '-n', '-c', '1', '-W', '1', '-I', interface, address],
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=2)
            return address if result.returncode == 0 else None
        except (OSError, subprocess.TimeoutExpired):
            return None
    with ThreadPoolExecutor(max_workers=32) as pool:
        for address in pool.map(probe, targets):
            if address:
                found[address] = ''
    if skipped:
        notes.append('Probe limit: skipped large IPv4 subnets ' + ', '.join(skipped))
    try:
        result = subprocess.run(['avahi-browse', '-a', '-r', '-t', '-p', '-k'],
                                capture_output=True, text=True, timeout=5)
        if result.returncode:
            notes.append('mDNS discovery: ' + result.stderr.strip())
        found.update(mdns_nodes(result.stdout, interface))
    except subprocess.TimeoutExpired as error:
        output = error.stdout or ''
        found.update(mdns_nodes(output.decode(errors='replace') if isinstance(output, bytes) else output, interface))
        notes.append('mDNS discovery timed out; retained available results.')
    except OSError as error:
        notes.append(f'mDNS discovery unavailable: {error}')
    return found, f'{len(targets)} IPv4 probes, {len(found)} responding/advertised addresses. ' + ' '.join(notes)


class LanMonitor:
    def __init__(self):
        self.network = NetworkMonitor()
        self.interfaces, self.neighbors = {}, []
        self.discovered, self.names = {}, hosts_names(read('/etc/hosts'))
        self.last_inventory, self.inventory_error = -float('inf'), ''
        self.lock = threading.Lock()

    def reset(self):
        self.network = NetworkMonitor()

    def remember(self, interface, nodes):
        with self.lock:
            for address, name in nodes.items():
                self.discovered[(interface, address)] = name
            self.last_inventory = -float('inf')

    def sample(self):
        now = time.monotonic()
        if now - self.last_inventory >= 5:
            try:
                self.interfaces = interfaces_from_json(ip_json('address', 'show'))
                self.neighbors = ip_json('neigh', 'show')
                self.inventory_error = ''
            except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired) as error:
                self.inventory_error = str(error)
            self.last_inventory = now
        snapshot = self.network.sample()
        with self.lock:
            discovered = dict(self.discovered)
        nodes = {}
        def node(interface, address):
            key = (interface, address)
            return nodes.setdefault(key, dict(interface=interface, address=address, name=self.names.get(address, ''),
                                              mac='', state='OBSERVED', source=set(), flows=[]))
        for interface, info in self.interfaces.items():
            for address in info['addresses']:
                row = node(interface, address)
                row.update(name=socket.gethostname(), mac=info['mac'], state='THIS HOST')
                row['source'].add('local')
        for entry in self.neighbors:
            if entry.get('dev') not in self.interfaces or not entry.get('lladdr'):
                continue  # Failed ARP entries with no MAC do not establish that a device exists.
            try:
                address = str(ipaddress.ip_address(entry['dst']))
            except (ValueError, KeyError):
                continue
            row = node(entry['dev'], address)
            states = entry.get('state', [])
            row.update(mac=entry['lladdr'], state='/'.join(states) if isinstance(states, list) else str(states))
            row['source'].add('neighbor cache')
        for (interface, address), name in discovered.items():
            if interface not in self.interfaces:
                continue
            row = node(interface, address)
            row['source'].add('discovery (cached)')
            if name:
                row['name'] = name
        local_map = {address: interface for interface, info in self.interfaces.items() for address in info['addresses']}
        for flow in snapshot['connections']:
            protocol, local, remote = flow[:3]
            own, peer = endpoint_address(local), endpoint_address(remote)
            interface = local_map.get(own)
            if interface is None or peer is None or peer in local_map:
                continue
            if not any(ipaddress.ip_address(peer) in network for network in self.interfaces[interface]['networks']):
                continue  # Internet peers must not be attributed to the LAN gateway's MAC.
            row = node(interface, peer)
            row['source'].add('socket')
            if protocol == 'tcp':
                row['flows'].append(flow)
        def total(flows, column):
            values = [flow[column] for flow in flows]
            return sum(values) if values and all(value is not None for value in values) else None
        records = []
        for row in nodes.values():
            flows = row['flows']
            records.append((row['interface'], row['address'], row['name'] or 'N/A', row['mac'] or 'N/A',
                            row['state'], total(flows, 4), total(flows, 3), len(flows), ', '.join(sorted(row['source']))))
        graph_rows = []
        for row in snapshot['interfaces']:
            if row[0] not in self.interfaces:
                continue
            flows = [flow for node_row in nodes.values() if node_row['interface'] == row[0] for flow in node_row['flows']]
            graph_rows.append((row[0], total(flows, 3), total(flows, 4), *row[3:]))
        return dict(nodes=records, interfaces=graph_rows, inventory=self.interfaces,
                    error='; '.join(error for error in (self.inventory_error, snapshot['error']) if error))
