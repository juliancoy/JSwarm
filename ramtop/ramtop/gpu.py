"""Intel i915 telemetry using perf PMUs, DRM sysfs and deduplicated fdinfo."""
from pathlib import Path
import os
import re
import time
import threading

from .hardware import Counter, Event, SYSFS, encode, read


def parse_fdinfo(text):
    fields = dict(line.split(':', 1) for line in text.splitlines() if ':' in line)
    fields = {key: value.strip() for key, value in fields.items()}
    if fields.get('drm-driver') != 'i915' or 'drm-client-id' not in fields:
        return None
    def number(value):
        parts = value.split()
        if not parts or not parts[0].isdecimal():
            return None
        factor = {'KiB': 1024, 'MiB': 1024**2, 'bytes': 1, 'ns': 1}.get(parts[1], 1) if len(parts) > 1 else 1
        return int(parts[0]) * factor
    metrics = {key: number(value) for key, value in fields.items()
               if key.startswith(('drm-total-', 'drm-shared-', 'drm-resident-', 'drm-active-',
                                  'drm-purgeable-', 'drm-engine-'))}
    return (fields.get('drm-pdev', 'unknown'), fields['drm-client-id']), metrics


def clients(proc=Path('/proc')):
    """One row per device/client id; shared descriptors are not counted twice."""
    found, denied = {}, 0
    for process in proc.glob('[0-9]*'):
        try:
            paths = os.scandir(process / 'fd')
        except PermissionError:
            denied += 1
            continue
        except OSError:
            continue
        name = read(process / 'comm', '?')
        with paths:
            for entry in paths:
                try:
                    # Socket-heavy daemons can hold hundreds of thousands of FDs.
                    # Only DRM character-device descriptors can carry GPU counters.
                    target = os.readlink(entry.path)
                    if not target.startswith('/dev/dri/'):
                        continue
                    parsed = parse_fdinfo((process / 'fdinfo' / entry.name).read_text())
                except PermissionError:
                    denied += 1
                    continue
                except OSError:
                    continue
                if not parsed:
                    continue
                key, metrics = parsed
                if key not in found:
                    found[key] = {'key': key, 'owners': set(), 'metrics': metrics}
                found[key]['owners'].add((int(process.name), name))
    return list(found.values()), denied


class GpuMonitor:
    def __init__(self, drm=Path('/sys/class/drm'), pmus=SYSFS, proc=Path('/proc')):
        self.lock = threading.RLock()
        self.proc, self.cards, self.counters, self.notes = proc, [], [], []
        self.last, self.previous_clients = time.monotonic(), {}
        for card in sorted(drm.glob('card[0-9]*')):
            if '-' in card.name or 'DRIVER=i915' not in read(card / 'device/uevent'):
                continue
            self.cards.append(card)
        # i915 PMUs expose encoded config aliases; rates of nanoseconds are utilization.
        pmu = pmus / 'i915'
        if pmu.exists():
            for path in sorted((pmu / 'events').glob('*')):
                if '.' in path.name:
                    continue
                expression = read(path)
                if not re.fullmatch(r'config=0x[0-9a-fA-F]+', expression):
                    continue
                unit = read(Path(str(path) + '.unit'))
                event = Event(path.name, int(read(pmu / 'type')), {'config': int(expression.split('=')[1], 0)}, 0,
                              unit=unit or 'events/s')
                self.open(event)
        for alias in ('energy-gpu', 'energy-pkg'):
            path = pmus / 'power' / 'events' / alias
            if path.exists() and read(Path(str(path) + '.unit')) == 'Joules':
                try:
                    scale = float(read(Path(str(path) + '.scale')))
                    self.open(Event(alias, int(read(pmus / 'power/type')), encode(pmus / 'power', read(path)),
                                    0, scale, 'W'))
                except (ValueError, KeyError) as error:
                    self.notes.append(str(error))
        if not self.cards:
            self.notes.append('No supported Intel i915 DRM card found; this view currently supports i915.')
        if len(self.cards) > 1:
            self.notes.append('PMU i915 readings cover its exposed domain; client rows identify each PCI device.')

    def open(self, event):
        try:
            self.counters.append(Counter(event))
        except OSError as error:
            self.notes.append(f'{event.label}: {error}')

    def sample(self):
        with self.lock:
            return self._sample()

    def _sample(self):
        now = time.monotonic()
        elapsed, self.last = now - self.last, now
        rates = {}
        for counter in self.counters:
            try:
                value = counter.sample(elapsed)
            except (OSError, ValueError):
                value = None
            if value is not None and counter.event.unit == 'ns':
                value = min(100., max(0., value / 1e7))
            rates[counter.event.label] = (value, '%' if counter.event.unit == 'ns' else counter.event.unit)
        data, denied = clients(self.proc) if self.cards else ([], 0)
        next_previous = {}
        for row in data:
            previous = self.previous_clients.get(row['key'], {})
            row['engines'] = {}
            for name, value in row['metrics'].items():
                if name.startswith('drm-engine-'):
                    old = previous.get(name)
                    row['engines'][name.removeprefix('drm-engine-')] = (
                        (value - old) / elapsed / 1e7 if old is not None and value is not None and value >= old else None)
            next_previous[row['key']] = row['metrics']
        self.previous_clients = next_previous
        sensors = []
        for card in self.cards:
            identity = read(card / 'device/uevent')
            sensors.append((f'{card.name} · i915 · {read(card / "device/device")}', identity))
            for path in sorted(card.glob('gt*_freq_mhz')):
                sensors.append((path.name, read(path) + ' MHz'))
            for path in sorted((card / 'device/hwmon').glob('hwmon*/*_input')):
                if path.name.startswith(('temp', 'power', 'energy', 'fan')):
                    unit = 'm°C' if path.name.startswith('temp') else 'µW' if path.name.startswith('power') else 'µJ' if path.name.startswith('energy') else 'RPM'
                    sensors.append((path.name, read(path) + ' ' + unit))
        return dict(rates=rates, clients=data, denied=denied, sensors=sensors, notes=self.notes)

    def close(self):
        with self.lock:
            for counter in self.counters:
                counter.close()
            self.counters.clear()
