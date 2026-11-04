import asyncio
from datetime import datetime

from rich.text import Text
from textual import work
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Static

from .sampling import capture, summarize_processes


class Hotspots(ModalScreen):
    BINDINGS = [('escape', 'dismiss', 'Close'), ('q', 'dismiss', 'Close'),
                ('r', 'capture', 'Capture again'), ('s', 'toggle_view', 'Processes / addresses')]
    CSS = '''
    Hotspots { align: center middle; background: #00000088; }
    #hotspot-box { width: 95%; height: 90%; border: thick #40bfa9; background: #172536; padding: 1; }
    #sample-actions { height: 3; min-height: 3; }
    #sample-actions Button { margin-right: 1; }
    #sample-summary { height: auto; margin-bottom: 1; }
    #sample-table { height: 1fr; }
    #sample-diagnostics { height: 4; overflow-y: auto; }
    '''

    def __init__(self, process_view=False):
        super().__init__()
        self.process_view = process_view
        self.sorts = {False: (6, True), True: (2, True)}

    def compose(self):
        with Vertical(id='hotspot-box'):
            yield Static('ADDRESS HOTSPOTS · sampled CPU accesses, not DRAM bandwidth\n'
                         'User-space, system-wide · virtual 4-KiB ranges grouped by PID\n'
                         'Loads: latency ≥30 cycles (use latency); stores: no latency estimate\n'
                         'R: new 3-second capture · Escape: return', id='sample-summary')
            with Horizontal(id='sample-actions'):
                yield Button('Processes [S]', id='sample-view', variant='primary')
                yield Button('Capture again [R]', id='sample-capture')
            yield DataTable(id='sample-table', zebra_stripes=True)
            yield Static(id='sample-diagnostics')

    def on_mount(self):
        self.rows = []
        self.configure_view()
        self.action_capture()

    @work
    async def action_capture(self):
        if getattr(self, "capturing", False):
            return
        self.capturing = True
        self.query_one('#sample-diagnostics', Static).update('Capturing for 3 seconds… UI remains responsive.')
        try:
            rows, ignored, diagnostics = await asyncio.to_thread(capture)
        except RuntimeError as error:
            self.capturing = False
            self.query_one('#sample-diagnostics', Static).update(Text(str(error)))
            return
        self.capturing = False
        self.rows = rows
        self.render_rows()
        count = sum(row.loads + row.stores for row in rows)
        self.query_one('#sample-diagnostics', Static).update(Text(
            f'{datetime.now():%H:%M:%S} · {count:,} samples · {len(rows):,} ranges · up to 500 rows in selected view · '
            f'{ignored:,} ignored/unknown/capped records\n'
            'Load/store sample counts have different selection bias; heat is sample count, not bytes.\n'
            + ('No address samples captured. ' if not rows else '') + diagnostics))

    PROCESS_LABELS = ('PID', 'PROCESS', 'LOAD SAMPLES', 'STORE SAMPLES', 'MEAN LOAD CYCLES', 'RANGES', 'TOTAL SAMPLES')

    LABELS = ('PID', 'PROCESS', 'VIRTUAL RANGE (4 KiB)', 'LOAD SAMPLES',
              'STORE SAMPLES', 'MEAN LOAD CYCLES', 'HEAT')

    def on_data_table_header_selected(self, event):
        event.stop()
        index = event.column_index
        numeric = index >= (2 if self.process_view else 3)
        self.sort_reverse = not self.sort_reverse if index == self.sort_column else numeric
        self.sort_column = index
        self.sorts[self.process_view] = (self.sort_column, self.sort_reverse)
        self.render_rows()

    def on_button_pressed(self, event: Button.Pressed):
        event.stop()
        if event.button.id == 'sample-view':
            self.action_toggle_view()
        elif event.button.id == 'sample-capture':
            self.action_capture()

    def action_toggle_view(self):
        self.process_view = not self.process_view
        self.configure_view()

    def configure_view(self):
        table = self.query_one(DataTable)
        table.clear(columns=True)
        table.add_columns(*(self.PROCESS_LABELS if self.process_view else self.LABELS))
        self.sort_column, self.sort_reverse = self.sorts[self.process_view]
        self.query_one('#sample-view', Button).label = 'Addresses [S]' if self.process_view else 'Processes [S]'
        self.query_one('#sample-summary', Static).update(
            ('PROCESS MEMORY SAMPLES' if self.process_view else 'ADDRESS HOTSPOTS')
            + ' · sampled CPU accesses, not DRAM bandwidth\n'
            'User-space, system-wide · 3-second capture · click headers to sort\n'
            'Loads: latency ≥30 cycles; stores: no latency estimate · counts have different biases\n'
            'S: switch processes / addresses · R: capture again · Escape: return')
        self.render_rows()

    def render_rows(self):
        table = self.query_one(DataTable)
        table.clear()
        def value(row):
            if self.process_view:
                return (row.pid, row.command.casefold(), row.loads, row.stores,
                        row.load_weight / row.loads if row.loads else 0, row.ranges,
                        row.loads + row.stores)[self.sort_column]
            return (row.pid, row.command.casefold(), row.address, row.loads, row.stores,
                    row.load_weight / row.loads if row.loads else 0, row.loads + row.stores)[self.sort_column]
        source = summarize_processes(self.rows) if self.process_view else self.rows
        rows = sorted(source, key=value, reverse=self.sort_reverse)
        if self.sort_column == (4 if self.process_view else 5):
            rows = [row for row in rows if row.loads] + [row for row in rows if not row.loads]
        peak = max((row.loads + row.stores for row in source), default=1)
        labels = self.PROCESS_LABELS if self.process_view else self.LABELS
        for column, index, label in zip(table.ordered_columns, range(7), labels):
            column.label = Text(label + (' ↓' if self.sort_reverse else ' ↑') if index == self.sort_column else label)
        for row in rows[:500]:
            latency = f'{row.load_weight / row.loads:.1f}' if row.loads else 'N/A'
            if self.process_view:
                table.add_row(str(row.pid), Text(row.command), str(row.loads), str(row.stores),
                              latency, str(row.ranges), str(row.loads + row.stores))
            else:
                heat = '█' * max(1, round((row.loads + row.stores) / peak * 16))
                table.add_row(str(row.pid), Text(row.command), f'{row.address:016x}–{row.address+4095:016x}',
                              str(row.loads), str(row.stores), latency, Text(heat, style='cyan'))
        for column in table.ordered_columns:
            column.content_width = max(column.content_width, len(column.label))
        table.refresh(layout=True)
