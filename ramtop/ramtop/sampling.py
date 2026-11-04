"""Bounded, opt-in perf memory sampling. No conversion of samples into bandwidth."""
from dataclasses import dataclass
from pathlib import Path
import os
import re
import subprocess
import tempfile

LINE = re.compile(r'^\s*(.*?)\s+(\d+)\s+(\S+):\s+([0-9a-fA-F]+)\s+(\d+)\s*$')


@dataclass
class Hotspot:
    pid: int
    command: str
    address: int
    loads: int = 0
    stores: int = 0
    load_weight: int = 0


def aggregate(lines):
    groups = {}
    ignored = 0
    for line in lines:
        match = LINE.match(line)
        if not match:
            if line.strip() and not line.startswith('#'):
                ignored += 1
            continue
        command, pid, event, address, weight = match.groups()
        operation = 'loads' if 'mem-loads' in event else 'stores' if 'mem-stores' in event else None
        address = int(address, 16)
        if not operation or not address:
            ignored += 1
            continue
        # Virtual addresses have meaning only within their process; group by 4-KiB range.
        key = (int(pid), command, address & ~4095)
        if key not in groups and len(groups) >= 100000:
            ignored += 1
            continue
        row = groups.setdefault(key, Hotspot(*key))
        setattr(row, operation, getattr(row, operation) + 1)
        if operation == 'loads':
            row.load_weight += int(weight)
    return sorted(groups.values(), key=lambda row: row.loads + row.stores, reverse=True), ignored


def capture(seconds=3):
    env = dict(os.environ, LC_ALL='C')
    with tempfile.TemporaryDirectory(prefix='ramtop-samples-') as directory:
        data = Path(directory) / 'perf.data'
        command = ['perf', 'mem', 'record', '-t', 'load,store', '--ldlat', '30', '-U',
                   '-a', '-c', '200003', '-m', '128', '-o', str(data), '--', 'sleep', str(seconds)]
        try:
            recorded = subprocess.run(command, capture_output=True, text=True, env=env, timeout=seconds + 12)
            if recorded.returncode:
                raise RuntimeError(recorded.stderr.strip()[-3000:] or 'perf mem recording failed')
            with (Path(directory) / 'samples.txt').open('w+') as decoded:
                report = subprocess.run(['perf', 'script', '-i', str(data), '-F', 'comm,pid,event,addr,weight'],
                                        stdout=decoded, stderr=subprocess.PIPE, text=True, env=env, timeout=15)
                if report.returncode:
                    raise RuntimeError(report.stderr.strip()[-3000:] or 'perf script decoding failed')
                decoded.seek(0)
                rows, ignored = aggregate(decoded)
            diagnostics = (recorded.stderr + '\n' + report.stderr).strip()[-4000:]
            return rows, ignored, diagnostics
        except (OSError, subprocess.TimeoutExpired) as error:
            raise RuntimeError(f'Memory sampling unavailable: {error}') from error


@dataclass
class ProcessSummary:
    pid: int
    command: str
    loads: int = 0
    stores: int = 0
    load_weight: int = 0
    ranges: int = 0


def summarize_processes(rows):
    """Summarize every retained range, before any UI top-500 limit, by PID."""
    groups, commands, addresses = {}, {}, {}
    for row in rows:
        summary = groups.setdefault(row.pid, ProcessSummary(row.pid, ''))
        summary.loads += row.loads
        summary.stores += row.stores
        summary.load_weight += row.load_weight
        commands.setdefault(row.pid, set()).add(row.command)
        addresses.setdefault(row.pid, set()).add(row.address)
    for pid, summary in groups.items():
        summary.command = ', '.join(sorted(commands[pid]))
        summary.ranges = len(addresses[pid])
    return list(groups.values())
