"""Read hardware LAN relationships without changing router switching state."""
import datetime
import re


def read(config):
    import pexpect
    child = pexpect.spawn('ssh', ['-p', str(config.get('ssh_port', 2222)),
        '-o', 'StrictHostKeyChecking=yes', '-o', 'ConnectTimeout=4',
        '-o', 'NumberOfPasswordPrompts=1',
        config['username']+'@'+config['address'],
        'head -n 4096 /proc/fcache/l2list'], encoding='utf-8', timeout=8)
    try:
        result = child.expect(['[Pp]assword:', pexpect.EOF])
        if result == 0:
            child.sendline(config['password'])
            child.expect(pexpect.EOF)
        body = child.before
        child.close()
        if child.exitstatus != 0 or 'MAC SA' not in body:
            raise RuntimeError('Hardware flow cache unavailable')
    finally:
        child.close(force=True)
    pairs = {}
    for line in body.splitlines():
        macs = re.findall(r'<([0-9a-fA-F:]{17})>', line)
        if len(macs) != 2:
            continue
        source, destination = [m.lower() for m in macs]
        if source == destination or int(destination[:2], 16) & 1:
            continue
        fields = line.split()
        try:
            idle = int(fields[1].rstrip(':'))
            hardware_packets = int(fields[7])
        except (ValueError, IndexError):
            continue
        if idle >= 300:
            continue
        pair = pairs.setdefault((source, destination), {'source_mac':source,
            'destination_mac':destination, 'hardware_packets':0, 'idle_seconds':idle})
        pair['hardware_packets'] += hardware_packets
        pair['idle_seconds'] = min(pair['idle_seconds'], idle)
    return {'observed':datetime.datetime.now(datetime.timezone.utc).isoformat(),
        'relationships':list(pairs.values()),
        'basis':'Router hardware L2 flow cache; MAC delivery pairs, not ultimate IP destinations. Hardware packet counts are cumulative, not bytes or Mbps. Coverage is partial.'}
