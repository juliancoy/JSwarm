"""Passive Ethernet/IP metadata only. Never store packet payloads."""
import datetime
import ipaddress
import json
import os
from pathlib import Path
import select
import socket
import struct
import time

IDLE_SECONDS = 300
MAX_PAIRS = 1024
DISPLAY_LIMIT = 75
SCOPE = ('Outbound IP bytes seen at gateway: LAN ingress before NAT; host egress only with an exact connected TCP/UDP socket match. '
         'Not physical links, capacity, replies or complete device activity. Switched peer traffic, other gateways, unconnected/short-lived host sockets and capture drops may be unseen. Unseen traffic is unknown.')


def parse_frame(frame, wire_length=None):
    """Return only IP metadata; tolerate snap truncation and reject bad headers."""
    if len(frame) < 14:
        return None
    dst_mac, src_mac = frame[:6], frame[6:12]
    if dst_mac[0] & 1 or src_mac[0] & 1:
        return None
    offset, kind = 14, int.from_bytes(frame[12:14], 'big')
    for _ in range(2):
        if kind not in (0x8100, 0x88a8):
            break
        if len(frame) < offset + 4:
            return None
        kind = int.from_bytes(frame[offset+2:offset+4], 'big'); offset += 4
    available = len(frame) - offset
    if kind == 0x0800:
        if available < 20 or frame[offset] >> 4 != 4:
            return None
        header = (frame[offset] & 15) * 4
        length = int.from_bytes(frame[offset+2:offset+4], 'big')
        if header < 20 or available < header or length < header:
            return None
        src = str(ipaddress.ip_address(frame[offset+12:offset+16]))
        dst = str(ipaddress.ip_address(frame[offset+16:offset+20]))
        protocol = frame[offset+9]
        fragment = int.from_bytes(frame[offset+6:offset+8], 'big') & 0x3fff
        transport = offset + header
    elif kind == 0x86dd:
        if available < 40 or frame[offset] >> 4 != 6:
            return None
        payload = int.from_bytes(frame[offset+4:offset+6], 'big')
        if not payload:  # Jumbo payload extension is unsupported.
            return None
        length = payload + 40
        src = str(ipaddress.ip_address(frame[offset+8:offset+24]))
        dst = str(ipaddress.ip_address(frame[offset+24:offset+40]))
        protocol, transport, fragment = frame[offset+6], offset+40, False
        for _ in range(8):
            if protocol not in (0, 43, 60, 44, 51):
                break
            if transport+2 > min(len(frame), offset+length):
                return None
            next_protocol = frame[transport]
            size = 8 if protocol == 44 else (frame[transport+1]+2)*4 if protocol == 51 else (frame[transport+1]+1)*8
            if transport+size > min(len(frame), offset+length):
                return None
            fragment |= protocol == 44
            transport += size; protocol = next_protocol
        if protocol in (0, 43, 60, 44, 51):
            return None
    else:
        return None
    if length > (wire_length if wire_length is not None else len(frame)) - offset:
        return None
    ports = None
    if not fragment and protocol in (6, 17) and transport+4 <= min(len(frame), offset+length):
        ports = struct.unpack('!HH', frame[transport:transport+4])
    tcp_payload = None
    ack_only = False
    if not fragment and protocol == 6 and transport+20 <= min(len(frame), offset+length):
        tcp_header = (frame[transport+12] >> 4)*4
        if tcp_header >= 20 and transport+tcp_header <= offset+length:
            tcp_payload = offset+length-transport-tcp_header
            ack_only = tcp_payload == 0 and frame[transport+13] == 0x10
    return {'tcp_payload_bytes':tcp_payload, 'tcp_ack_only':ack_only, 'source_ip': src, 'destination': dst, 'source_mac': src_mac.hex(':'),
            'destination_mac': dst_mac.hex(':'), 'ip_bytes': length,
            'protocol': protocol, 'socket': (protocol, src, ports[0], dst, ports[1]) if ports else None}


def internet_destination(address, networks):
    ip = ipaddress.ip_address(address)
    return ip.is_global and not ip.is_multicast and not any(ip.version == net.version and ip in net for net in networks)


def configured_internet(address, config):
    # Classification is stable for the configuration interval; avoid reparsing
    # every local subnet for every packet on a busy interface.
    if '_networks' not in config:
        return internet_destination(address, [ipaddress.ip_network(n) for n in config['local_networks']])
    cache = config['_internet_cache']
    if address not in cache:
        value = internet_destination(address, config['_networks'])
        if len(cache) < 8192:
            cache[address] = value
        return value
    return cache[address]


def socket_tuples():
    """Kernel socket endpoints distinguish host-generated traffic from NAT forwarding."""
    result = set()
    def endpoint(value, v6):
        address, port = value.split(':')
        raw = bytes.fromhex(address)
        raw = b''.join(raw[i:i+4][::-1] for i in range(0, len(raw), 4))
        parsed = ipaddress.ip_address(raw)
        return str(getattr(parsed, 'ipv4_mapped', None) or parsed), int(port, 16)
    for name, protocol in (('tcp', 6), ('udp', 17), ('tcp6', 6), ('udp6', 17)):
        try:
            with open('/proc/net/'+name) as file:
                next(file)
                for line in file:
                    columns = line.split()
                    src, sport = endpoint(columns[1], name.endswith('6'))
                    dst, dport = endpoint(columns[2], name.endswith('6'))
                    if dport and not ipaddress.ip_address(dst).is_unspecified:
                        if len(result) < 8192:
                            result.add((protocol, src, sport, dst, dport))
        except (OSError, ValueError, IndexError):
            continue
    return result


def attribute(packet, interface, outgoing, config, sockets):
    if interface == config['lan_interface'] and not outgoing:
        info = config['interfaces'][interface]
        local = [ipaddress.ip_network(n) for n in info['networks']]
        src = ipaddress.ip_address(packet['source_ip'])
        if (packet['destination_mac'] == info['mac'] and packet['source_mac'] != info['mac']
                and not src.is_multicast and not src.is_unspecified
                and any(src.version == n.version and src in n for n in local)):
            return packet['source_mac'], 'LAN ingress source MAC and IP; before NAT'
    if (outgoing and packet['socket'] in sockets and packet['source_ip'] in config['host_addresses']
            and packet['source_mac'] == config['interfaces'].get(interface, {}).get('mac')):
        return config['host_mac'], 'Host egress; exact connected kernel socket tuple'
    return None


def flow_observation(packet, interface, outgoing, config, sockets):
    """Attribute each Internet packet once, on the client side of NAT or a host socket."""
    info = config['interfaces'].get(interface, {})
    upload = configured_internet(packet['destination'], config)
    download = configured_internet(packet['source_ip'], config)
    if upload == download:
        return None
    direction = 'upload' if upload else 'download'
    local_ip = packet['source_ip'] if upload else packet['destination']
    remote_ip = packet['destination'] if upload else packet['source_ip']
    key = packet['socket']
    normalized = key if upload or key is None else (key[0], key[3], key[4], key[1], key[2])
    device = None
    if interface == config['lan_interface']:
        local = ipaddress.ip_address(local_ip)
        local_networks = [ipaddress.ip_network(n) for n in info.get('networks', [])]
        in_lan = any(local.version == n.version and local in n for n in local_networks)
        if upload and not outgoing and in_lan and packet['destination_mac'] == info.get('mac') and packet['source_mac'] != info.get('mac'):
            device = packet['source_mac']
        elif download and outgoing and in_lan and packet['source_mac'] == info.get('mac') and packet['destination_mac'] != info.get('mac'):
            device = packet['destination_mac']
    if device:
        basis = 'LAN ingress before NAT' if upload else 'LAN egress after NAT'
    elif (normalized in sockets and local_ip in config['host_addresses']
          and (upload and outgoing and packet['source_mac'] == info.get('mac')
               or download and not outgoing and packet['destination_mac'] == info.get('mac'))):
        device = config['host_mac']
        basis = 'Host exact connected kernel socket match'
    else:
        return None
    return {'device_mac': device, 'local_ip': local_ip, 'remote_ip': remote_ip,
            'local_port': normalized[2] if normalized else None,
            'remote_port': normalized[4] if normalized else None,
            'protocol': packet['protocol'], 'direction': direction, 'interface': interface, 'basis': basis}


def uplink_observation(packet, interface, outgoing, config, sockets):
    """Independent uplink ledger: never add this to client-side NAT totals."""
    if interface == config['lan_interface']:
        return None
    upload = configured_internet(packet['destination'], config)
    download = configured_internet(packet['source_ip'], config)
    if upload == download or outgoing != upload:
        return None
    info = config['interfaces'].get(interface, {})
    if packet['source_mac' if upload else 'destination_mac'] != info.get('mac'):
        return None
    known = flow_observation(packet, interface, outgoing, config, sockets)
    if known:
        return known
    key = packet['socket']
    normalized = key if upload or key is None else (key[0], key[3], key[4], key[1], key[2])
    return {'device_mac':'uplink-unattributed',
            'local_ip':packet['source_ip'] if upload else packet['destination'],
            'remote_ip':packet['destination'] if upload else packet['source_ip'],
            'local_port':normalized[2] if normalized else None,
            'remote_port':normalized[4] if normalized else None,
            'protocol':packet['protocol'], 'direction':'upload' if upload else 'download',
            'interface':interface, 'basis':'Uplink packet; host, container or forwarded owner unknown'}


class InterfaceRates:
    def __init__(self, root='/sys/class/net'):
        self.root = Path(root)
        self.previous = {}

    def sample(self, interfaces, now):
        result = {}
        for interface in interfaces:
            try:
                values = tuple(int((self.root/interface/'statistics'/k).read_text()) for k in ('tx_bytes','rx_bytes'))
            except (OSError, ValueError):
                continue
            old = self.previous.get(interface)
            self.previous[interface] = (now, values)
            if old is None or now <= old[0] or any(a < b for a,b in zip(values,old[1])):
                continue
            result[interface] = {direction+'_mbps':(value-before)*8/(now-old[0])/1e6
                                 for direction,value,before in zip(('upload','download'),values,old[1])}
        return result


class Flows:
    """Bounded bidirectional 5-tuple counters; no payload, DNS-query, or TLS content."""
    def __init__(self):
        self.records = {}
        self.evictions = 0

    def observe(self, flow, count, now, stamp, packet=None):
        key = tuple(flow[k] for k in ('device_mac','local_ip','remote_ip','local_port','remote_port','protocol','interface'))
        if key not in self.records:
            if len(self.records) >= MAX_PAIRS:
                del self.records[min(self.records, key=lambda k:self.records[k]['_seen'])]
                self.evictions += 1
            self.records[key] = {k:v for k,v in flow.items() if k != 'direction'}
            self.records[key].update(first_seen=stamp, upload_bytes=0, download_bytes=0, _upload=0, _download=0)
        row = self.records[key]
        row[flow['direction']+'_basis'] = flow['basis']
        row['last_seen'] = stamp
        row['_seen'] = now
        row[flow['direction']+'_bytes'] += count
        row['_'+flow['direction']] += count
        if packet:
            for kind, value in (('tcp_payload',packet.get('tcp_payload_bytes') or 0),
                                ('tcp_ack_only',count if packet.get('tcp_ack_only') else 0)):
                field = '_'+flow['direction']+'_'+kind
                row[field] = row.get(field,0)+value

    def view(self, now, elapsed):
        self.records = {k:v for k,v in self.records.items() if now-v['_seen'] < IDLE_SECONDS}
        rows = []
        for record in self.records.values():
            row = {k:v for k,v in record.items() if not k.startswith('_')}
            for direction in ('upload', 'download'):
                row[direction+'_mbps'] = record['_'+direction]*8/elapsed/1e6
                record['_'+direction] = 0
                for kind in ('tcp_payload','tcp_ack_only'):
                    field = '_'+direction+'_'+kind
                    row[direction+'_'+kind+'_mbps'] = record.get(field,0)*8/elapsed/1e6
                    record[field] = 0
            rows.append(row)
        return {'records':rows, 'evictions':self.evictions, 'max_flows':MAX_PAIRS, 'idle_seconds':IDLE_SECONDS,
                'scope':'Observed Internet IP bytes including IP headers. Client side of NAT for LAN devices; exact connected socket matches for this host. Excludes switched LAN traffic, other gateways, unconnected host UDP and missed short-lived sockets. Rates use the latest capture interval; retained bytes cover each record lifetime, not the displayed chart window. Hosting provider and PTR names do not identify an application.'}


class Destinations:
    def __init__(self):
        self.records = {}
        self.evictions = 0

    def expire(self, now):
        self.records = {k:v for k,v in self.records.items() if now-v['_seen'] < IDLE_SECONDS}

    def observe(self, packet, source, basis, interface, now, stamp):
        key = (source, packet['destination'])
        if key not in self.records:
            if len(self.records) >= MAX_PAIRS:
                del self.records[min(self.records, key=lambda k: self.records[k]['_seen'])]
                self.evictions += 1
            self.records[key] = {'source_mac':source, 'destination':packet['destination'], 'bytes':0,
                                 'first_seen':stamp, '_sample':0}
        record = self.records[key]
        record.update(source_ip=packet['source_ip'], interface=interface, basis=basis, last_seen=stamp, _seen=now)
        record['bytes'] += packet['ip_bytes']; record['_sample'] += packet['ip_bytes']

    def view(self, now, elapsed):
        self.expire(now)
        rows = []
        for record in self.records.values():
            rows.append({**{k:v for k,v in record.items() if not k.startswith('_')},
                         'mbps':record['_sample']*8/elapsed/1e6})
            record['_sample'] = 0
        return {'relationships':rows, 'idle_seconds':IDLE_SECONDS, 'max_pairs':MAX_PAIRS,
                'display_limit':DISPLAY_LIMIT, 'evictions':self.evictions, 'scope':SCOPE}


def main():
    sockets, connections, config = {}, set(), None
    records, counts, flows = Destinations(), {}, Flows()
    uplink_flows, interface_rates = Flows(), InterfaceRates()
    uplink_history = []
    previous = time.monotonic(); refreshed = 0; sockets_refreshed = 0
    dropped_total = 0
    buffer = bytearray(256)
    networks = []
    config_path = Path('/state/capture-config.json')
    while True:
        now = time.monotonic()
        if now-refreshed >= 1:
            try:
                candidate = json.loads(config_path.read_text())
                if time.time()-candidate['updated_epoch'] > 90:
                    raise ValueError('Capture network configuration is stale')
                config = candidate
                for interface in list(sockets):
                    if interface not in config['interfaces']:
                        sockets.pop(interface).close()
                for interface in config['interfaces']:
                    if interface not in sockets:
                        sock = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(3))
                        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4*1024*1024)
                        sock.bind((interface, 0)); sock.setblocking(False); sockets[interface] = sock
                networks = [ipaddress.ip_network(n) for n in config['local_networks']]
                config['_networks'] = tuple(networks)
                config['_internet_cache'] = {}
                error = None
            except (OSError, ValueError, KeyError) as exc:
                config = None; error = str(exc)
            refreshed = now
        if config is not None and now-sockets_refreshed>=.25:
            connections=socket_tuples();sockets_refreshed=now
        ready, _, _ = select.select(list(sockets.values()), [], [], .1)
        for sock in ready:
            # Bounded snapshot: headers only; Python discards bytes after parsing.
            try:
                wire_length, _, _, address = sock.recvmsg_into([buffer], 0, socket.MSG_TRUNC)
                frame = bytes(buffer[:min(wire_length, len(buffer))])
            except BlockingIOError:
                continue
            if config is None:
                continue
            interface, outgoing = address[0], address[2] == socket.PACKET_OUTGOING
            # PACKET_OTHERHOST is not proof of gateway ingress.
            if not outgoing and address[2] != socket.PACKET_HOST:
                continue
            if interface == config['lan_interface'] and len(frame) >= 14:
                local = bytes.fromhex(config['interfaces'][interface]['mac'].replace(':', ''))
                peer = frame[:6] if outgoing and frame[6:12] == local else frame[6:12] if not outgoing and frame[:6] == local else None
                if peer and not peer[0] & 1:
                    mac = peer.hex(':')
                    if mac in counts or len(counts) < MAX_PAIRS:
                        entry = counts.setdefault(mac, [0,0]); entry[1 if outgoing else 0] += wire_length
            packet = parse_frame(frame, wire_length)
            if not packet:
                continue
            flow = flow_observation(packet, interface, outgoing, config, connections)
            if flow:
                flows.observe(flow, packet['ip_bytes'], now, datetime.datetime.now(datetime.timezone.utc).isoformat(), packet)
            uplink = uplink_observation(packet, interface, outgoing, config, connections)
            if uplink:
                uplink_flows.observe(uplink, packet['ip_bytes'], now, datetime.datetime.now(datetime.timezone.utc).isoformat(), packet)
            if configured_internet(packet['destination'], config):
                source = attribute(packet, interface, outgoing, config, connections)
                if source:
                    stamp = datetime.datetime.now(datetime.timezone.utc).isoformat()
                    records.observe(packet, *source, interface, now, stamp)
        now = time.monotonic()
        if now-previous >= 1:
            elapsed = now-previous
            stamp = datetime.datetime.now(datetime.timezone.utc).isoformat()
            capture_stats = {}
            for name, sock in sockets.items():
                try:
                    packets, drops = struct.unpack('II', sock.getsockopt(263, 6, 8))
                    capture_stats[name] = {'packets': packets, 'kernel_drops': drops}
                    dropped_total += drops
                except OSError:
                    capture_stats[name] = {'packets': None, 'kernel_drops': None}
            data = {'capture_statistics': capture_stats, 'kernel_drops_since_start': dropped_total, 'interface':'enp7s0', 'peers':{mac:{'rx_mbps':v[0]*8/elapsed/1e6,'tx_mbps':v[1]*8/elapsed/1e6} for mac,v in counts.items()},
                    'sample_seconds':elapsed, 'updated':stamp, 'basis':'Gateway-observed Ethernet frame bytes; excludes switched traffic',
                    'outbound':records.view(now, elapsed), 'flows':flows.view(now, elapsed), 'capture_error':error}
            data['uplink_flows'] = uplink_flows.view(now, elapsed)
            data['uplink_flows']['scope'] = 'Independent uplink IP ledger; includes unmatched packets. Never sum with client-side NAT flows.'
            data['interface_rates'] = interface_rates.sample(sockets, now)
            data['uplink_interfaces'] = [name for name in sockets if config and name != config['lan_interface']]
            if not uplink_history or now-uplink_history[-1]['monotonic'] >= 5:
                uplink_history.append({'updated':stamp,'monotonic':now,'interface_rates':data['interface_rates'],
                    'internet_rates':{direction:sum(r[direction+'_mbps'] for r in data['uplink_flows']['records']) for direction in ('upload','download')}})
            uplink_history = [row for row in uplink_history if now-row['monotonic'] <= 1800]
            data['uplink_history'] = [{k:v for k,v in row.items() if k != 'monotonic'} for row in uplink_history]
            data['outbound'].update(updated=stamp, interfaces=list(sockets), local_networks=config['local_networks'] if config else [], available=config is not None)
            temporary = Path('/state/link-traffic.tmp'); temporary.write_text(json.dumps(data)); os.replace(temporary,'/state/link-traffic.json')
            counts = {}; previous = now


if __name__ == '__main__':
    main()
