"""Linux disk and network rates; independent counters and explicit scopes."""
from pathlib import Path
import os
import re
import subprocess
import time
import psutil
from .connections import parse_ss


def rate(current, previous, elapsed):
    if current is None or previous is None or current < previous or elapsed <= 0:
        return None
    return (current - previous) / elapsed


class DiskMonitor:
    def __init__(self):
        self.last, self.devices, self.processes = time.monotonic(), {}, {}

    def sample(self):
        now = time.monotonic()
        elapsed, self.last = now - self.last, now
        current = psutil.disk_io_counters(perdisk=True, nowrap=False) or {}
        devices = []
        for name, counter in current.items():
            old = self.devices.get(name)
            def change(field):
                return rate(getattr(counter, field, None), getattr(old, field, None), elapsed)
            read_ops, write_ops = change('read_count'), change('write_count')
            read_ms, write_ms = change('read_time'), change('write_time')
            busy = change('busy_time')
            block = Path('/sys/class/block') / name
            kind = 'partition' if (block / 'partition').exists() else 'stacked' if list((block / 'slaves').glob('*')) else 'device'
            devices.append((name, change('read_bytes'), change('write_bytes'), read_ops, write_ops,
                            read_ms / read_ops if read_ops else None, write_ms / write_ops if write_ops else None,
                            min(100., busy / 10) if busy is not None else None, kind))
        self.devices = current
        processes, previous, denied = [], {}, 0
        for process in psutil.process_iter(['pid', 'name', 'username', 'create_time']):
            try:
                info = process.info
                key = (process.pid, info['create_time'])
                io = process.io_counters()
                old = self.processes.get(key) if info['create_time'] is not None else None
                def change(field):
                    return rate(getattr(io, field, None), getattr(old, field, None), elapsed)
                # Cancellation is a separate /proc field; never silently subtract it from writes.
                try:
                    fields = dict(line.split(':', 1) for line in (Path('/proc') / str(process.pid) / 'io').read_text().splitlines())
                    cancelled = int(fields.get('cancelled_write_bytes', '0'))
                except (OSError, ValueError):
                    cancelled = None
                if info['create_time'] is not None:
                    previous[key] = io
                processes.append((process.pid, info['username'] or '?', info['name'] or '?',
                                  change('read_bytes'), change('write_bytes'), change('read_chars'), change('write_chars'), cancelled))
            except psutil.AccessDenied:
                denied += 1
            except (psutil.NoSuchProcess, psutil.ZombieProcess):
                continue
        self.processes = previous
        mounts = []
        for partition in psutil.disk_partitions(all=False):
            try:
                usage = psutil.disk_usage(partition.mountpoint)
                mounts.append((partition.device, partition.mountpoint, partition.fstype, usage.used, usage.free, usage.percent))
            except OSError:
                continue
        return dict(devices=devices, processes=processes, mounts=mounts, denied=denied)




class NetworkMonitor:
    def __init__(self):
        self.last, self.interfaces, self.flows = time.monotonic(), {}, {}

    def sample(self):
        now = time.monotonic()
        elapsed, self.last = now - self.last, now
        current = psutil.net_io_counters(pernic=True, nowrap=False) or {}
        stats = psutil.net_if_stats()
        interfaces = []
        for name, counter in current.items():
            old = self.interfaces.get(name)
            def change(field):
                return rate(getattr(counter, field, None), getattr(old, field, None), elapsed)
            link = stats.get(name)
            interfaces.append((name, change('bytes_recv'), change('bytes_sent'), change('packets_recv'),
                               change('packets_sent'), change('errin'), change('errout'), change('dropin'), change('dropout'),
                               'up' if link and link.isup else 'down', link.speed if link and link.speed > 0 else None,
                               link.mtu if link else None))
        self.interfaces = current
        error = ''
        try:
            result = subprocess.run(['ss', '-tunipeHO'], capture_output=True, text=True,
                                    env=dict(os.environ, LC_ALL='C'), timeout=4)
            if result.returncode:
                raise RuntimeError(result.stderr.strip() or 'ss failed')
            flows = parse_ss(result.stdout)
        except (OSError, subprocess.TimeoutExpired, RuntimeError) as failure:
            flows, error = [], str(failure)
        connections, previous = [], {}
        for flow in flows:
            old = self.flows.get(flow['key'], {})
            # Stable socket identity is required; endpoints alone can be reused.
            identifiable = flow['key'][1] not in (None, 0) or flow['key'][2] not in (None, '0')
            tx = rate(flow['tx'], old.get('tx'), elapsed) if identifiable else None
            rx = rate(flow['rx'], old.get('rx'), elapsed) if identifiable else None
            previous[flow['key']] = flow
            owners = ', '.join(f'{pid} {name}' for pid, name in flow['owners']) or 'N/A (restricted/unowned)'
            connections.append((flow['protocol'], flow['local'], flow['remote'], rx, tx, flow['rtt'],
                                flow['recvq'], flow['sendq'], flow['state'], owners))
        self.flows = previous
        return dict(interfaces=interfaces, connections=connections, error=error)
