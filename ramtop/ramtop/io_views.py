import asyncio
from rich.text import Text
from collections import deque

from textual import work
from textual.containers import Horizontal, Vertical
from textual.widgets import Input, Select, Sparkline, Static, TabbedContent, TabPane
from .io_metrics import DiskMonitor, NetworkMonitor
from .metric_table import MetricTable


class IoPanel(Vertical):
    DEFAULT_CSS = '''
    IoPanel { height: 1fr; }
    IoPanel Select { height: 3; }
    IoPanel Input { height: 3; }
    .io-graphs { height: 6; min-height: 6; }
    .io-graphs Vertical { width: 1fr; border: round #35536b; padding: 0 1; }
    .io-graphs Static { height: 2; }
    .io-graphs Sparkline { height: 2; color: #40d6b0; }
    .io-note { height: auto; max-height: 5; overflow-y: auto; }
    IoPanel TabbedContent { height: 1fr; }
    IoPanel MetricTable { height: 1fr; min-height: 4; }
    '''
    kind = ''

    def make_monitor(self):
        return DiskMonitor() if self.kind == 'disk' else NetworkMonitor()

    def on_mount(self):
        self.monitor = self.make_monitor()
        self.histories = [deque(maxlen=120), deque(maxlen=120)]
        self.snapshot = None
        self.options = []
        self.refresh_io()
        # One lightweight scheduler follows the app's current refresh interval.
        self.next_sample = 0
        self.io_timer = self.set_interval(.25, self.schedule_sample)

    def schedule_sample(self):
        import time
        now = time.monotonic()
        if now >= self.next_sample:
            self.next_sample = now + self.app.interval
            self.refresh_io()

    @work
    async def refresh_io(self):
        if self.app.query_one('#main-tabs', TabbedContent).active != self.kind + '-tab':
            self.needs_baseline = True
            return
        if getattr(self, 'sampling', False):
            return
        self.sampling = True
        if getattr(self, 'needs_baseline', False):
            self.monitor = self.make_monitor()
            for history in self.histories:
                history.clear()
            self.needs_baseline = False
        try:
            data = await asyncio.to_thread(self.monitor.sample)
        except (OSError, RuntimeError) as error:
            self.query_one(f'#{self.kind}-note', Static).update(Text(str(error)))
            return
        finally:
            self.sampling = False
        if not self.is_mounted or self.app.frozen:
            return
        self.snapshot = data
        self.render_snapshot()

    def on_select_changed(self, event):
        if event.select.id == f'{self.kind}-device':
            for history in getattr(self, 'histories', []):
                history.clear()
            if getattr(self, 'snapshot', None):
                self.render_snapshot(append=True)

    def on_input_changed(self, event):
        if getattr(self, 'snapshot', None):
            self.render_snapshot(append=False)

    def update_graphs(self, records, append):
        from .app import size
        selector = self.query_one(Select)
        names = [row[0] for row in records]
        if names != self.options:
            previous = selector.value
            selector.set_options([(name, name) for name in names])
            self.options = names
            if previous in names:
                selector.value = previous
            elif names:
                if self.kind == 'disk':
                    preferred = [row[0] for row in records if row[-1] == 'device' and not row[0].startswith(('loop', 'ram', 'zram'))]
                else:
                    preferred = [row[0] for row in records if row[9] == 'up' and row[0] != 'lo']
                selector.value = (preferred or names)[0]
        chosen = next((row for row in records if row[0] == selector.value), None)
        for index, direction in enumerate(('read', 'write') if self.kind == 'disk' else ('receive', 'send')):
            value = chosen[index+1] if chosen else None
            history = self.histories[index]
            graph = self.query_one(f'#{self.kind}-graph-{index}', Sparkline)
            graph.display = value is not None
            if value is None:
                history.clear()
            elif append:
                history.append(value)
            graph.data = list(history)
            self.query_one(f'#{self.kind}-label-{index}', Static).update(
                f'{direction.upper()} · {selector.value if chosen else "no device"}\n'
                + (size(value) + '/s' if value is not None else 'N/A · warming up / unavailable'))

    def on_unmount(self):
        if hasattr(self, 'io_timer'):
            self.io_timer.stop()


def graphs(kind):
    with Horizontal(classes='io-graphs'):
        for index in range(2):
            with Vertical():
                yield Static(id=f'{kind}-label-{index}')
                yield Sparkline([], id=f'{kind}-graph-{index}')


class DiskPanel(IoPanel):
    kind = 'disk'

    def compose(self):
        yield Select([], prompt='Choose disk / block device', id='disk-device')
        yield from graphs('disk')
        yield Static('Block-device throughput; select one layer to avoid double-counting partitions / device-mapper.', classes='io-note')
        with TabbedContent():
            with TabPane('Devices'):
                yield MetricTable(list(zip(('DEVICE', 'READ/s', 'WRITE/s', 'READ IOPS', 'WRITE IOPS', 'READ ms/op', 'WRITE ms/op', 'BUSY', 'LAYER'),
                                           ('text', 'rate', 'rate', 'float', 'float', 'float', 'float', 'percent', 'text'))), id='disk-devices', sort=2, reverse=True)
            with TabPane('Processes · iotop style'):
                yield Input(placeholder='Filter PID, user or process', id='disk-filter')
                yield MetricTable(list(zip(('PID', 'USER', 'PROCESS', 'DISK READ/s', 'DISK WRITE/s', 'READ CHARS/s', 'WRITE CHARS/s', 'CANCELLED TOTAL'),
                                           ('int', 'text', 'text', 'rate', 'rate', 'rate', 'rate', 'bytes'))), id='disk-processes', sort=4, reverse=True)
            with TabPane('Filesystems'):
                yield MetricTable(list(zip(('DEVICE', 'MOUNT', 'TYPE', 'USED', 'FREE', 'USED %'),
                                           ('text', 'text', 'text', 'bytes', 'bytes', 'percent'))), id='disk-mounts', sort=5, reverse=True)
        yield Static(id='disk-note', classes='io-note')

    def render_snapshot(self, append=True):
        data = self.snapshot
        self.update_graphs(data['devices'], append)
        self.query_one('#disk-devices', MetricTable).set_records(data['devices'])
        query = self.query_one('#disk-filter', Input).value.casefold()
        self.query_one('#disk-processes', MetricTable).set_records(
            [row for row in data['processes'] if query in ' '.join(str(value) for value in row[:3]).casefold()])
        self.query_one('#disk-mounts', MetricTable).set_records(data['mounts'])
        self.query_one('#disk-note', Static).update(
            f'{data["denied"]} processes denied I/O accounting. New/reset counters show N/A.\n'
            'Process writes are kernel storage accounting; buffering/cancellation means they need not match device writes.\n'
            'CHAR rates include cached/other I/O. BUSY is time with I/O, not device capacity; latency is mean completed I/O.')


class NetworkPanel(IoPanel):
    kind = 'network'

    def compose(self):
        yield Select([], prompt='Choose network interface', id='network-device')
        yield from graphs('network')
        yield Static('Interface RX/TX counters · virtual interfaces / loopback can overlap; interfaces are never summed.', classes='io-note')
        with TabbedContent():
            with TabPane('Interfaces'):
                yield MetricTable(list(zip(('INTERFACE', 'RX/s', 'TX/s', 'RX PACKETS/s', 'TX PACKETS/s', 'RX ERR/s', 'TX ERR/s', 'RX DROP/s', 'TX DROP/s', 'LINK', 'Mbps', 'MTU'),
                                           ('text', 'rate', 'rate', 'float', 'float', 'float', 'float', 'float', 'float', 'text', 'int', 'int'))), id='network-interfaces', sort=1, reverse=True)
            with TabPane('Connections · iftop style'):
                yield Input(placeholder='Filter address, port, PID, process or protocol', id='network-filter')
                yield MetricTable(list(zip(('PROTO', 'LOCAL', 'REMOTE', 'TCP RX/s', 'TCP ACKED TX/s', 'RTT ms', 'RECV-Q', 'SEND-Q', 'STATE', 'PID / PROCESS'),
                                           ('text', 'text', 'text', 'rate', 'rate', 'float', 'bytes', 'bytes', 'text', 'text'))), id='network-connections', sort=3, reverse=True)
        yield Static(id='network-note', classes='io-note')

    def render_snapshot(self, append=True):
        data = self.snapshot
        self.update_graphs(data['interfaces'], append)
        self.query_one('#network-interfaces', MetricTable).set_records(data['interfaces'])
        query = self.query_one('#network-filter', Input).value.casefold()
        self.query_one('#network-connections', MetricTable).set_records(
            [row for row in data['connections'] if query in ' '.join(str(v) for v in row).casefold()])
        self.query_one('#network-note', Static).update(
            ('Socket telemetry unavailable: ' + data['error'] + '\n' if data['error'] else '')
            + 'TCP rates: received / acknowledged payload from ss, not packet-capture wire throughput. UDP rates: N/A.\n'
            'Short-lived sockets may be missed; missing counters and inaccessible owners show N/A. No DNS lookups or packet capture.')
