"""Read-only PMU discovery and perf counters; never infer bytes from misses."""
from dataclasses import dataclass
from pathlib import Path
import ctypes
import math
import os
import platform
import struct
import subprocess

SYSFS = Path('/sys/bus/event_source/devices')


def read(path, default=''):
    try:
        return Path(path).read_text().strip()
    except OSError:
        return default


def cpus(value):
    result = []
    for part in value.split(','):
        if not part.strip():
            continue
        ends = part.split('-')
        result.extend(range(int(ends[0]), int(ends[-1]) + 1))
    return result


def encode(pmu, expression):
    """Pack named fields according to the kernel's format ABI, including sparse bits."""
    configs = dict(config=0, config1=0, config2=0)
    for term in expression.split(','):
        field, _, value = term.partition('=')
        register, bits = read(pmu / 'format' / field).split(':')
        positions = cpus(bits)
        number = int(value or '1', 0)
        if number >> len(positions):
            raise ValueError(f'{field} exceeds format width')
        for source, target in enumerate(positions):
            configs[register] |= ((number >> source) & 1) << target
    return configs


@dataclass
class Event:
    label: str
    kind: int
    config: dict
    cpu: int
    scale: float = 1
    unit: str = 'events/s'
    tier: int = 4


class Counter:
    def __init__(self, event):
        self.event = event
        self.previous = None
        syscall = {'x86_64': 298, 'aarch64': 241}.get(platform.machine())
        if syscall is None:
            raise OSError('perf_event_open unsupported on this architecture')
        attr = bytearray(128)
        struct.pack_into('IIQ', attr, 0, event.kind, len(attr), event.config['config'])
        struct.pack_into('Q', attr, 32, 3)  # TOTAL_TIME_ENABLED | TOTAL_TIME_RUNNING
        struct.pack_into('QQ', attr, 56, event.config.get('config1', 0), event.config.get('config2', 0))
        buffer = (ctypes.c_char * len(attr)).from_buffer(attr)
        libc = ctypes.CDLL(None, use_errno=True)
        libc.syscall.restype = ctypes.c_long
        self.fd = libc.syscall(ctypes.c_long(syscall), ctypes.byref(buffer), ctypes.c_int(-1),
                               ctypes.c_int(event.cpu), ctypes.c_int(-1), ctypes.c_ulong(8))
        if self.fd < 0:
            error = ctypes.get_errno()
            raise OSError(error, os.strerror(error))

    def sample(self, elapsed):
        current = struct.unpack('QQQ', os.read(self.fd, 24))
        previous, self.previous = self.previous, current
        if previous is None:
            return None
        value, enabled, running = (a-b for a, b in zip(current, previous))
        if min(value, enabled, running) < 0 or running == 0 or elapsed <= 0:
            return None
        return value * enabled / running * self.event.scale / elapsed

    def close(self):
        os.close(self.fd)


def discover(root=SYSFS):
    events, notes = [], []
    for pmu in sorted(root.glob('uncore_imc*')):
        try:
            kind = int(read(pmu / 'type'))
            representatives = cpus(read(pmu / 'cpumask'))
            if not representatives:
                raise ValueError('no representative CPUs')
            # These aliases describe IMC traffic. Require explicit kernel units/scaling.
            for name in ('cas_count_read', 'cas_count_write', 'data_reads', 'data_writes', 'data_read', 'data_write',
                         'ia_requests', 'gt_requests', 'io_requests'):
                path = pmu / 'events' / name
                if not path.exists():
                    continue
                if name in ('ia_requests', 'gt_requests', 'io_requests'):
                    # Intel documents partial-write merging: raw requests are not bytes.
                    for cpu in representatives:
                        events.append(Event(f'{pmu.name}@cpu{cpu}/{name}', kind,
                                            encode(pmu, read(path)), cpu, 1, 'requests/s', 5))
                    continue
                unit = read(Path(str(path) + '.unit'))
                scale = float(read(Path(str(path) + '.scale'), 'nan'))
                factor = {'Bytes': 1, 'bytes': 1, 'B': 1, 'KiB': 1024, 'MiB': 1024**2, 'GiB': 1024**3}.get(unit)
                if factor is None or not math.isfinite(scale) or scale <= 0:
                    notes.append(f'{pmu.name}/{name}: skipped; no validated byte scale/unit')
                    continue
                tier = 1 if name.startswith('cas_count_') and 'free_running' not in pmu.name else 2
                for cpu in representatives:
                    events.append(Event(f'{pmu.name}@cpu{cpu}/{name}', kind, encode(pmu, read(path)), cpu,
                                        scale * factor, 'B/s', tier))
        except (ValueError, KeyError, OSError) as error:
            notes.append(f'{pmu.name}: {error}')
    return events, notes


class MBMCounter:
    """Root resctrl group only; excludes tasks assigned to other control/monitor groups."""
    def __init__(self, path):
        self.path, self.previous = path, None
        self.event = Event(f'resctrl root group/{path.parent.name}/{path.name}', 0, {}, 0, unit='B/s', tier=3)

    def sample(self, elapsed):
        raw = read(self.path)
        value = int(raw) if raw.isdecimal() else None
        previous, self.previous = self.previous, value
        if value is None or previous is None or value < previous or elapsed <= 0:
            return None
        return (value - previous) / elapsed

    def close(self):
        pass


class Hardware:
    def __init__(self, root=SYSFS, resctrl=Path('/sys/fs/resctrl')):
        self.counters = []
        cpu_info = read('/proc/cpuinfo').split('\n\n')[0]
        self.vendor = next((line.split(':', 1)[1].strip() for line in cpu_info.splitlines() if line.startswith('vendor_id')), 'unknown')
        self.events, self.notes = discover(root)
        self.inventory = {p.name: sorted(x.name for x in (p / 'events').glob('*') if '.' not in x.name)
                          for p in sorted(root.iterdir())} if root.exists() else {}
        for tier in (1, 2):
            for event in (e for e in self.events if e.tier == tier):
                self.open(event)
            if self.counters:
                break
        if not self.counters:
            for path in sorted((resctrl / 'mon_data').glob('mon_*/mbm_*_bytes')):
                self.counters.append(MBMCounter(path))
        if not self.counters:
            if 'cqm_mbm_total' in cpu_info and not (resctrl / 'mon_data').exists():
                self.notes.append('CPU advertises MBM, but resctrl monitoring is not mounted/exposed. '
                                  'An administrator can inspect resctrl support; ramtop does not mount it.')
            if not self.events:
                self.notes.append('No supported IMC byte-counter aliases exposed. Cache event rates are not DRAM bandwidth.')
            # Generic hardware counts are explicitly events/s, never converted to bytes.
            for name, config in [('cache references', 2), ('cache misses', 3)]:
                for cpu in cpus(read('/sys/devices/system/cpu/online', '0')):
                    self.open(Event(f'cpu{cpu}/{name}', 0, {'config': config}, cpu))
        self.tier = min((c.event.tier for c in self.counters), default=4)
        for event in (e for e in self.events if e.tier == 5):
            self.open(event)
        self.notes = list(dict.fromkeys(self.notes))
        if not self.counters:
            self.notes.append('Hardware counters unavailable. OS statistics remain live.')

    def open(self, event):
        try:
            self.counters.append(Counter(event))
        except OSError as error:
            self.notes.append(f'{event.label}: {error.strerror or error}')

    def sample(self, elapsed):
        result = []
        for counter in self.counters:
            try:
                value = counter.sample(elapsed)
            except (OSError, ValueError):
                value = None
            result.append((counter.event, value))
        return result

    def close(self):
        for counter in self.counters:
            counter.close()
        self.counters.clear()

    def report(self):
        info = read('/proc/cpuinfo').split('\n\n')[0]
        try:
            listing = subprocess.run(['perf', 'list', '--no-desc'], capture_output=True, text=True, timeout=5)
            perf = (listing.stdout + listing.stderr)[:60000]
        except (OSError, subprocess.TimeoutExpired) as error:
            perf = str(error)
        return dict(cpu=info, vendor=self.vendor, tier=self.tier, pmus=self.inventory,
                    selected=[c.event.label for c in self.counters], notes=self.notes,
                    perf_event_paranoid=read('/proc/sys/kernel/perf_event_paranoid'), perf_list=perf)
