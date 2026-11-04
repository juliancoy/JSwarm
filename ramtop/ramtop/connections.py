"""Dependency-free parser for Linux ss socket telemetry."""
import re


def parse_ss(output):
    rows = []
    for line in output.splitlines():
        fields = line.split()
        if len(fields) < 6 or fields[0] not in ('tcp', 'udp'):
            continue
        protocol, state, recvq, sendq, local, remote = fields[:6]
        if not recvq.isdecimal() or not sendq.isdecimal():
            continue
        def number(name):
            match = re.search(r'\b' + name + r':(\d+)', line)
            return int(match[1]) if match else None
        inode = number('ino')
        cookie = re.search(r'\bsk:([0-9a-f]+)', line)
        owners = sorted(set((int(pid), name) for name, pid in re.findall(r'\("([^"]+)",pid=(\d+)', line)))
        rtt = re.search(r'\brtt:([\d.]+)', line)
        rows.append(dict(key=(protocol, inode, cookie[1] if cookie else None, local, remote), protocol=protocol,
                         state=state, local=local, remote=remote, owners=owners, recvq=int(recvq), sendq=int(sendq),
                         tx=number('bytes_acked') if protocol == 'tcp' else None,
                         rx=number('bytes_received') if protocol == 'tcp' else None,
                         rtt=float(rtt[1]) if rtt else None))
    return rows

