import asyncio
import threading
from collections import deque

from rich.text import Text
from textual import work
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Button, DataTable, Sparkline, Static, TabbedContent, TabPane

from .gpu import GpuMonitor


def memory(metrics, prefix):
    values = [value for key, value in metrics.items() if key.startswith(prefix) and value is not None]
    return sum(values) if values else None


class GpuView(Vertical):
    BINDINGS = []
    DEFAULT_CSS = '''
    GpuView { height: 1fr; }
    #gpu-box { width: 100%; height: 1fr; background: #172536; padding: 0 1; }
    #gpu-heading { height: 2; }
    #gpu-tabs { height: 1fr; }
    #gpu-traffic { height: 7; min-height: 7; }
    #gpu-traffic Vertical { width: 1fr; border: round #35536b; padding: 0 1; }
    #gpu-traffic Static { height: 3; }
    #gpu-traffic Sparkline { height: 2; color: #b69aff; }
    #gpu-description { height: auto; }
    #gpu-engines { height: 1fr; min-height: 6; }
    #gpu-clients { height: 1fr; min-height: 6; }
    #gpu-client-note { height: auto; }
    #gpu-sensors, #gpu-errors { height: auto; }
    #gpu-close { height: 3; }
    '''

    def compose(self):
        with Vertical(id='gpu-box'):
            yield Static('[bold cyan]GPU · INTEL GRAPHICS[/] · live utilization and memory activity', id='gpu-heading')
            with TabbedContent(id='gpu-tabs'):
                with TabPane('Overview', id='gpu-overview'):
                    with Horizontal(id='gpu-traffic'):
                        for name in ('read', 'write', 'requests'):
                            with Vertical():
                                yield Static(id=f'gpu-{name}-label')
                                yield Sparkline([], id=f'gpu-{name}-graph')
                    yield Static('Read/write bandwidth: N/A · GPU-only directional counters are not exposed.\n'
                                 'GT requests combine reads + writes; partial writes may merge. Not bytes/s.\n'
                                 'Engine activity may overlap; engine percentages must not be summed.', id='gpu-description')
                    yield DataTable(id='gpu-engines', zebra_stripes=True)
                with TabPane('GPU clients / memory', id='gpu-client-tab'):
                    yield Static('Shared system RAM, not dedicated VRAM. Memory is allocation/residency, not traffic.\n'
                                 'One row per DRM client; shared buffers may appear in multiple clients. Click headers to sort.', id='gpu-client-note')
                    yield DataTable(id='gpu-clients', zebra_stripes=True)
                with TabPane('Sensors / access', id='gpu-sensor-tab'):
                    with VerticalScroll():
                        yield Static(id='gpu-sensors')
                        yield Static(id='gpu-errors')

    def on_mount(self):
        self.query_one('#gpu-engines', DataTable).add_columns('METRIC', 'VALUE', 'UNIT / SCOPE', 'ACTIVITY')
        self.query_one('#gpu-clients', DataTable).add_columns('PID / PROCESS', 'DEVICE / CLIENT', 'ALLOCATED',
                                                           'RESIDENT', 'SHARED', 'ACTIVE', 'PURGEABLE', 'ENGINE BUSY')
        self.monitor = None
        self.request_history = deque(maxlen=120)
        self.client_sort, self.client_reverse, self.client_rows = 3, True, []
        self.metric_rows, self.metric_sort, self.metric_reverse = [], 0, False
        for direction in ('read', 'write'):
            self.query_one(f'#gpu-{direction}-label', Static).update(f'[bold]{direction.upper()} · GPU MEMORY[/]\nN/A · no directional counter')
            self.query_one(f'#gpu-{direction}-graph').display = False
        self.next_sample = 0
        self.refresh_gpu()
        self.gpu_timer = self.set_interval(.25, self.schedule_sample)

    def schedule_sample(self):
        import time
        now = time.monotonic()
        if now >= self.next_sample:
            self.next_sample = now + self.app.interval
            self.refresh_gpu()

    @work
    async def refresh_gpu(self):
        if self.app.query_one('#main-tabs', TabbedContent).active != 'gpu-tab':
            self.needs_baseline = True
            return
        if getattr(self, "refreshing", False):
            return
        self.refreshing = True
        try:
            if self.monitor is None:
                self.monitor = GpuMonitor()
            if getattr(self, 'needs_baseline', False):
                for counter in getattr(self.monitor, 'counters', []):
                    counter.previous = None
                if hasattr(self.monitor, 'previous_clients'):
                    self.monitor.previous_clients = {}
                self.request_history.clear()
                self.needs_baseline = False
            snapshot = await asyncio.to_thread(self.monitor.sample)
        except (OSError, ValueError) as error:
            self.refreshing = False
            self.query_one('#gpu-errors', Static).update(Text(str(error)))
            return
        self.refreshing = False
        if not self.is_mounted or self.app.frozen:
            return
        self.render_snapshot(snapshot)

    def render_snapshot(self, snapshot):
        from .app import size
        samples = getattr(self.app, 'last_hardware_samples', [])
        values = [value for event, value in samples if event.unit == 'requests/s' and event.label.endswith('/gt_requests')]
        value = sum(values) if values and all(v is not None for v in values) else None
        graph = self.query_one('#gpu-requests-graph', Sparkline)
        graph.display = value is not None
        if value is None:
            self.request_history.clear()
        else:
            self.request_history.append(value)
        graph.data = list(self.request_history)
        self.query_one('#gpu-requests-label', Static).update(
            'GT MEMORY REQUESTS · R+W\n' + (f'{value:,.0f} requests/s' if value is not None else 'N/A · unavailable / warming up'))
        self.metric_rows = []
        descriptions = {'energy-gpu': 'W · GPU energy domain', 'energy-pkg': 'W · whole CPU package',
                        'actual-frequency': 'MHz · wall-time average incl. idle', 'requested-frequency': 'MHz · wall-time average incl. idle',
                        'rc6-residency': '% · low-power residency', 'software-gt-awake-time': '% · software awake',
                        'interrupts': 'interrupts/s'}
        for name, (value, unit) in snapshot['rates'].items():
            display = 'N/A' if value is None else f'{value:,.2f}'
            self.metric_rows.append((name, value, descriptions.get(name, '% · engine time' if unit == '%' else unit), unit))
        self.render_metrics()
        self.client_rows = []
        for row in snapshot['clients']:
            owners = ', '.join(f'{pid} {name}' for pid, name in sorted(row['owners']))
            numbers = [memory(row['metrics'], prefix) for prefix in
                       ('drm-total-', 'drm-resident-', 'drm-shared-', 'drm-active-', 'drm-purgeable-')]
            engines = ', '.join(f'{name} {value:.1f}%' if value is not None else f'{name} N/A'
                                for name, value in row['engines'].items())
            maximum = max((v for v in row['engines'].values() if v is not None), default=None)
            self.client_rows.append((owners, '/'.join(row['key']), *numbers, engines, maximum))
        self.render_clients()
        sensors = '\n\n'.join(f'{name}\n{value}' for name, value in snapshot['sensors']) or 'No GPU sysfs sensors available.'
        self.query_one('#gpu-sensors', Static).update(Text(sensors))
        self.query_one('#gpu-errors', Static).update(Text(
            '\nACCESS / COVERAGE\n' + '\n'.join(snapshot['notes'] or ['No GPU PMU open errors.'])
            + f'\n{snapshot["denied"]} process directories or descriptors denied this scan.'
            + '\nClient coverage is limited to readable fdinfo; duplicate client descriptors are deduplicated.'
            + '\nGPU-only read/write bandwidth and physical-chip activity are not available through these counters.'))

    def render_clients(self):
        from .app import size
        table = self.query_one('#gpu-clients', DataTable)
        table.clear()
        valid = [row for row in self.client_rows if row[self.client_sort] is not None]
        missing = [row for row in self.client_rows if row[self.client_sort] is None]
        def sort_value(row):
            if self.client_sort == 0:
                return int(row[0].split()[0])
            return row[8] if self.client_sort == 7 else row[self.client_sort]
        if self.client_sort == 7:
            valid = [row for row in self.client_rows if row[8] is not None]
            missing = [row for row in self.client_rows if row[8] is None]
        valid.sort(key=sort_value, reverse=self.client_reverse)
        labels = ('PID / PROCESS', 'DEVICE / CLIENT', 'ALLOCATED', 'RESIDENT', 'SHARED', 'ACTIVE', 'PURGEABLE', 'ENGINE BUSY')
        for index, column in enumerate(table.ordered_columns):
            column.label = Text(labels[index] + (' ↓' if self.client_reverse else ' ↑') if index == self.client_sort else labels[index])
            column.content_width = max(column.content_width, len(column.label))
        for row in valid + missing:
            table.add_row(Text(row[0]), row[1], *('N/A' if v is None else size(v) for v in row[2:7]), row[7])
        table.refresh(layout=True)

    def on_data_table_header_selected(self, event):
        event.stop()
        if event.data_table.id == 'gpu-clients':
            index = event.column_index
            self.client_reverse = not self.client_reverse if index == self.client_sort else index >= 2
            self.client_sort = index
            self.render_clients()
        elif event.data_table.id == 'gpu-engines':
            index = event.column_index
            self.metric_reverse = not self.metric_reverse if index == self.metric_sort else index in (1, 3)
            self.metric_sort = index
            self.render_metrics()

    def render_metrics(self):
        table = self.query_one('#gpu-engines', DataTable)
        table.clear()
        def value(row):
            if self.metric_sort in (1, 3):
                return row[1] if self.metric_sort == 1 or row[3] == '%' else -1
            return row[0] if self.metric_sort == 0 else row[2]
        rows = [r for r in self.metric_rows if r[1] is not None]
        rows.sort(key=value, reverse=self.metric_reverse)
        rows += [r for r in self.metric_rows if r[1] is None]
        for index, (column, label) in enumerate(zip(table.ordered_columns, ('METRIC', 'VALUE', 'UNIT / SCOPE', 'ACTIVITY'))):
            column.label = Text(label + (' ↓' if self.metric_reverse else ' ↑') if index == self.metric_sort else label)
            column.content_width = max(column.content_width, len(column.label))
        for name, number, unit, original in rows:
            bar = Text('N/A', style='dim')
            if number is not None and original == '%':
                count = min(20, max(0, round(number / 5)))
                bar = Text('|' * count, style='green')
                bar.append('·' * (20-count), style='dim')
            table.add_row(name, 'N/A' if number is None else f'{number:,.2f}', unit,
                          bar if original == '%' else Text('—', style='dim'))
        if not rows:
            table.add_row('GPU PMU counters', 'N/A', 'See Sensors / access', '')
        table.refresh(layout=True)

    def on_button_pressed(self, event):
        if event.button.id == 'gpu-close':
            event.stop()
            self.app.action_tab('memory-tab')

    def on_unmount(self):
        if hasattr(self, 'gpu_timer'):
            self.gpu_timer.stop()
        if getattr(self, 'monitor', None):
            threading.Thread(target=self.monitor.close, daemon=True).start()
