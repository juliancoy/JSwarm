"""Read-only link information and unprivileged UDP route probes."""
import ipaddress
import re
import select
import socket
import struct
import subprocess
import time
from pathlib import Path


def link_info(interface):
    result = {'interface': interface, 'capacity_mbps': None, 'capacity_basis': 'Unknown'}
    try:
        speed = float(Path('/sys/class/net/'+interface+'/speed').read_text())
        if speed > 0:
            result.update(capacity_mbps=speed, capacity_basis='Negotiated Ethernet link rate')
    except (OSError, ValueError):
        pass
    try:
        process = subprocess.run(['iw', 'dev', interface, 'link'], capture_output=True, text=True, timeout=2)
        text = process.stdout
        bssid = re.search(r'Connected to ([0-9a-f:]{17})', text, re.I)
        if bssid:
            result['bssid'] = bssid.group(1).lower()
        for direction in ('rx', 'tx'):
            bitrate = re.search(direction+r' bitrate:\s*([\d.]+) MBit/s', text)
            if bitrate:
                result[direction+'_link_mbps'] = float(bitrate.group(1))
        rates = [result[d+'_link_mbps'] for d in ('rx', 'tx') if d+'_link_mbps' in result]
        if rates:
            result.update(capacity_mbps=max(rates), capacity_basis='Reported Wi-Fi PHY rate; not available bandwidth')
    except (OSError, subprocess.TimeoutExpired):
        pass
    return result


def route_trace(target='1.1.1.1', max_hops=12, timeout=.45):
    hops = []
    for ttl in range(1, max_hops+1):
        hop = {'ttl': ttl, 'ip': None, 'rtt_ms': None}
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
                probe.setsockopt(socket.SOL_IP, 11, 1)  # IP_RECVERR
                probe.setsockopt(socket.SOL_IP, socket.IP_TTL, ttl)
                probe.setblocking(False)
                start = time.monotonic()
                probe.sendto(b'lan-map', (target, 33434+ttl))
                if select.select([probe], [], [], timeout)[0]:
                    _, ancillary, _, _ = probe.recvmsg(512, 512, 8192)  # MSG_ERRQUEUE
                    for level, kind, value in ancillary:
                        if level == socket.SOL_IP and kind == 11 and len(value) >= 24:
                            _, origin, icmp_type, icmp_code, _, _, _ = struct.unpack('=IBBBBII', value[:16])
                            if origin == 2:  # SO_EE_ORIGIN_ICMP
                                hop.update(ip=socket.inet_ntoa(value[20:24]), rtt_ms=round((time.monotonic()-start)*1000, 2), icmp_type=icmp_type, icmp_code=icmp_code)
        except OSError as exc:
            hop['error'] = str(exc)
        hops.append(hop)
        if hop['ip'] == target or (hop.get('icmp_type') == 3 and hop.get('icmp_code') != 3):
            break
    return {'target': target, 'method': 'UDP TTL probes with ICMP error queue', 'hops': hops,
            'complete': bool(hops and hops[-1]['ip'] == target)}
