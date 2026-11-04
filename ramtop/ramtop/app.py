from collections import deque
import time

import psutil
from rich.text import Text
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Footer, Header, Input, Sparkline, Static, TabbedContent, TabPane

from .hardware import Hardware, read
from .cpu import CpuMonitor, cpu_display
from .memory import cache_rates, mbm_rates, pressure, memory_fields
from .gpu_view import GpuView
from .io_views import DiskPanel, NetworkPanel
from .lan_view import LanPanel


def size(value):
    for unit in ('B', 'KiB', 'MiB', 'GiB', 'TiB'):
        if abs(value) < 1024 or unit == 'TiB':
            return f'{value:,.1f} {unit}'
        value /= 1024


def dram_rates(samples):
    """Sum observed IMC domains per direction; incomplete readings remain unknown."""
    rates = {}
    for direction, aliases in {
        'read': {'cas_count_read', 'data_reads', 'data_read'},
        'write': {'cas_count_write', 'data_writes', 'data_write'},
    }.items():
        values = [value for event, value in samples
                  if event.tier in (1, 2) and event.unit == 'B/s'
                  and event.label.rsplit('/', 1)[-1] in aliases]
        rates[direction] = sum(values) if values and all(v is not None for v in values) else None
    return rates


def meter(percent, width=24):
    filled = min(width, max(0, round(percent / 100 * width)))
    return f'[green]{"▰" * filled}[/][#35465b]{"▱" * (width-filled)}[/] {percent:5.1f}%'


class Help(ModalScreen):
    BINDINGS = [('escape', 'dismiss'), ('f1', 'dismiss'), ('q', 'dismiss')]

    def compose(self):
        with VerticalScroll(id='help-box'):
            yield Static('[bold cyan]ramtop · measurement guide[/]\n\n'
                         'Tabs 1–6: Memory, CPU, GPU, Disk, Network, LAN. C/G select CPU/GPU.\n'
                         'Disk: device throughput and iotop-style process I/O accounting.\n'
                         'Network: interface rates and iftop-style TCP endpoint rates via ss.\n'
                         'TCP payload rates differ from interface bytes; UDP rates are unavailable.\n'
                         'Space freezes the display; counters continue sampling.\n'
                         '+ / − changes the sampling interval. M sorts by RSS; P by CPU.\n'
                         '/ filters processes by name or PID. Escape clears the filter.\n'
                         'D shows diagnostics. A captures address hotspots. Q exits.\n\n'
                         'CPU/GPU/I/O graphs count IMC requests, not bytes: partial writes can merge.\n'
                         'Address heat shows sampled CPU accesses by PID and virtual range.\n'
                         'Loads are latency-filtered; sample counts cannot measure bandwidth.\n\n'
                         '[bold]Bandwidth[/]: only kernel-scaled IMC bytes are DRAM traffic.\n'
                         'CAS PMUs show individual controller/channel counter domains; their names\n'
                         'are kernel identifiers, not DIMM slot labels. Aggregate IMC data is never\n'
                         'divided into guessed channels. Missing values appear as N/A.\n\n'
                         '[bold]RDT MBM[/]: root resctrl group traffic between LLC and memory,\n'
                         'not per-channel DRAM reads/writes. Local and total overlap; never sum them.\n'
                         'Tasks assigned to other resctrl groups are outside this scope.\n\n'
                         '[bold]Fallback[/]: cache events, page faults and swap are separate metrics.\n'
                         'RSS is resident memory, not process bandwidth. No saturation percentage\n'
                         'is inferred without a validated platform bandwidth ceiling.\n\n'
                         '[dim]Escape to return[/]')


class Diagnostics(ModalScreen):
    BINDINGS = [('escape', 'dismiss'), ('q', 'dismiss'), ('d', 'dismiss')]

    def compose(self):
        hardware = self.app.hardware
        with VerticalScroll(id='help-box'):
            yield Static(Text('DISCOVERY / ACCESS\n\n' + '\n'.join(hardware.notes or ['No discovery errors.'])
                              + '\n\nCOUNTER SAMPLES\n' + '\n'.join(f'{event.label}: {value if value is not None else "N/A"} {event.unit}' for event, value in getattr(self.app, 'last_hardware_samples', []))
                              + '\n\nPMUs: ' + ', '.join(hardware.inventory)
                              + '\n\nperf_event_paranoid: ' + read('/proc/sys/kernel/perf_event_paranoid')
                              + '\n\nFor system-wide perf, an administrator may need to grant CAP_PERFMON\n'
                              'or run this trusted installation with sufficient privileges.\n'
                              'For Intel IMC discovery, check whether intel_uncore is loaded.\n'
                              'ramtop never changes permissions or loads modules.\n\nEscape to return'))


class Ramtop(App):
    TITLE = 'ramtop'
    SUB_TITLE = 'system performance'
    CSS = '''
    Screen { background: #101722; color: #d6e2ef; }
    Header { background: #193449; }
    Footer { background: #193449; }
    #main-tabs { height: 1fr; }
    #main-tabs > ContentSwitcher { height: 1fr; }
    #main-tabs > ContentSwitcher > TabPane { padding: 0 1; }
    #body { height: 1fr; padding: 0 1; overflow-y: auto; }
    #toolbar { height: 3; min-height: 3; }
    #cpu-toggle { min-width: 24; height: 3; }
    #cpu-panel { height: auto; max-height: 22; border: round #35536b; padding: 0 1; }
    #cpu-title, #cpu-legend, #cpu-bars { height: auto; }
    #overview { height: 7; }
    .card { border: round #35536b; padding: 0 1; width: 1fr; }
    #hardware { height: 5; overflow-y: auto; border: round #2b8291; padding: 0 1; }
    #bandwidth { height: 7; min-height: 7; }
    #cache-performance, #mbm-performance { height: 6; min-height: 6; }
    #cache-performance Sparkline { color: #a7adff; }
    #mbm-performance Sparkline { color: #40d6b0; }
    #memory-pressure { height: 2; color: #b5c7d9; }
    #memory-tab.no-dram #bandwidth { height: 4; min-height: 4; }
    #memory-tab.short.no-dram #bandwidth { display: none; }
    #memory-tab.short #hardware { height: 3; }
    #memory-tab.short #overview { height: 7; }
    #memory-tab.short #cache-performance, #memory-tab.short #mbm-performance { height: 5; min-height: 5; }
    #memory-tab.short #cache-performance Sparkline, #memory-tab.short #mbm-performance Sparkline { height: 1; }
    #memory-tab.short #agents { height: 5; min-height: 5; }
    #memory-tab.short #agents Sparkline { height: 1; }
    #memory-tab.short #trend { display: none; }
    #memory-tab.tiny #toolbar, #memory-tab.tiny #status, #memory-tab.tiny #memory-pressure { display: none; }
    #memory-tab.tiny #hardware { height: 2; border: none; padding: 0; }
    #memory-tab.tiny #agents { display: none; }
    #memory-tab.tiny #bandwidth { height: 5; min-height: 5; }
    #memory-tab.tiny #bandwidth Static { height: 1; }
    #memory-tab.tiny #cache-performance, #memory-tab.tiny #mbm-performance { height: 4; min-height: 4; }
    #memory-tab.tiny #cache-performance Static, #memory-tab.tiny #mbm-performance Static { height: 1; }
    #agents { height: 7; min-height: 7; }
    #agents Sparkline { color: #a9a0ff; }
    .bandwidth-card { width: 1fr; border: round #35536b; padding: 0 1; }
    .bandwidth-card Static { height: 2; }
    .bandwidth-card Sparkline { height: 3; }
    #read-graph { color: #40d6b0; }
    #write-graph { color: #f6b55e; }
    #trend { height: 2; color: #7896ba; }
    #status { height: 2; color: #e9c46a; }
    #search { height: 3; }
    DataTable { height: 1fr; min-height: 3; }
    #help-box { width: 85%; height: 85%; border: thick #40bfa9; background: #172536; padding: 1 2; }
    Help, Diagnostics { align: center middle; background: #00000088; }
    Screen.compact #overview { height: 14; layout: vertical; }
    Screen.compact .card { height: 7; width: 1fr; }
    '''
    BINDINGS = [('1', "tab('memory-tab')", 'Memory'), ('2', "tab('cpu-tab')", 'CPU'),
                ('3', "tab('gpu-tab')", 'GPU'), ('4', "tab('disk-tab')", 'Disk'),
                ('5', "tab('network-tab')", 'Network'), ('6', "tab('lan-tab')", 'LAN'), ('q', 'quit', 'Quit'), ('f10', 'quit', 'Quit'), ('f1', 'help', 'Help'),
                ('g', 'gpu', 'GPU'), ('b', 'process_samples', 'Process samples'), ('c', 'toggle_cpu', 'CPU meters'), ('a', 'hotspots', 'Addresses'), ('space', 'freeze', 'Freeze'), ('d', 'diagnostics', 'Discovery'),
                ('m', 'sort_memory', 'RSS'), ('p', 'sort_cpu', 'CPU'),
                ('/', 'filter', 'Filter'), ('escape', 'clear_filter', 'Clear'),
                ('+', 'faster', 'Faster'), ('-', 'slower', 'Slower')]

    def __init__(self, hardware=None, interval=1.0):
        super().__init__()
        self.hardware = hardware or Hardware()
        self.interval, self.frozen, self.sort_key = interval, False, 'memory_info'
        self.sort_reverse = True
        self.process_rows = []
        self.history = deque(maxlen=120)
        self.bandwidth_history = {direction: deque(maxlen=120) for direction in ('read', 'write', 'ia', 'gt', 'io')}
        self.last = time.monotonic()
        self.old_vm = psutil.virtual_memory()
        self.cpu_model = next((line.split(':', 1)[1].strip() for line in read('/proc/cpuinfo').splitlines()
                               if line.startswith('model name')), 'Linux CPU')
        self.performance_histories = {key: deque(maxlen=120) for key in
                                      ('cache references', 'cache misses', 'mbm_total_bytes', 'mbm_local_bytes')}
        self.old_swap = psutil.swap_memory()
        self.old_faults = self.vmstats()
        self.processes = {}
        self.cpu_monitor = CpuMonitor()
        self.cpu_visible = True
        self.cpu_sort_key, self.cpu_sort_reverse = 'cpu', True

    @staticmethod
    def vmstats():
        return {k: int(v) for k, v in (line.split() for line in read('/proc/vmstat').splitlines())}

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with TabbedContent(id='main-tabs'):
            with TabPane('Memory', id='memory-tab'):
                with Vertical(id='body'):
                    with Horizontal(id='toolbar'):
                        yield Button('Process samples [B]', id='process-samples')
                    with Horizontal(id='overview'):
                        yield Static(id='memory', classes='card')
                        yield Static(id='system', classes='card')
                    yield Static(id='hardware')
                    with Horizontal(id='bandwidth'):
                        with Vertical(classes='bandwidth-card'):
                            yield Static(id='read-label')
                            yield Sparkline([], id='read-graph')
                        with Vertical(classes='bandwidth-card'):
                            yield Static(id='write-label')
                            yield Sparkline([], id='write-graph')
                    with Horizontal(id='agents'):
                        for agent, title in [('ia', 'CPU · IA'), ('gt', 'GPU · GT'), ('io', 'I/O')]:
                            with Vertical(classes='bandwidth-card'):
                                yield Static(title, id=f'{agent}-label')
                                yield Sparkline([], id=f'{agent}-graph')
                    for group, keys in [('cache', ('references', 'misses')), ('mbm', ('total', 'local'))]:
                        with Horizontal(id=f'{group}-performance'):
                            for key in keys:
                                with Vertical(classes='bandwidth-card'):
                                    yield Static(id=f'{group}-{key}-label')
                                    yield Sparkline([], id=f'{group}-{key}-graph')
                    yield Static(id='memory-pressure')
                    yield Sparkline([], id='trend')
                    yield Static(id='status')
                    yield Input(placeholder='Filter process name or PID…   / to focus · Escape to clear', id='search')
                    yield DataTable(id='processes', zebra_stripes=True, cursor_type='row')
            with TabPane('CPU', id='cpu-tab'):
                with VerticalScroll(id='cpu-panel'):
                    yield Static(id='cpu-title')
                    yield Static(id='cpu-bars')
                    yield Static('[green]| User[/]  [red]| System/IRQ[/]  [blue]| Nice[/]  '
                                 '[yellow]| Steal[/]  [cyan]| I/O wait[/]  · % excludes idle and I/O wait', id='cpu-legend')
                yield Input(placeholder='Filter CPU processes by name or PID', id='cpu-search')
                yield DataTable(id='cpu-processes', zebra_stripes=True, cursor_type='row')
            with TabPane('GPU', id='gpu-tab'):
                yield GpuView()
            with TabPane('Disk', id='disk-tab'):
                yield DiskPanel()
            with TabPane('Network', id='network-tab'):
                yield NetworkPanel()
            with TabPane('LAN', id='lan-tab'):
                yield LanPanel()
        yield Footer()

    def on_mount(self):
        table = self.query_one('#processes', DataTable)
        table.focus()
        for selector in ('#processes', '#cpu-processes'):
            self.query_one(selector, DataTable).add_columns(*self.PROCESS_LABELS)
        self.set_process_sort(self.sort_key, self.sort_reverse)
        self.hardware.sample(1)
        self.timer = self.set_interval(self.interval, self.tick)
        self.tick()
        self.set_process_sort('cpu', True, cpu=True)

    def on_resize(self, event):
        self.default_screen.set_class(event.size.width < 75, "compact")
        if self.query('#memory-tab'):
            self.query_one('#memory-tab').set_class(event.size.height < 40, 'short')
            self.query_one('#memory-tab').set_class(event.size.height < 30, 'tiny')
        if self.cpu_visible:
            self.call_after_refresh(self.update_cpu_panel)

    def tick(self):
        if not self.query('#memory'):
            return
        now = time.monotonic()
        elapsed, self.last = now - self.last, now
        samples = self.hardware.sample(elapsed)
        self.last_hardware_samples = samples
        self.cpu_monitor.sample()
        vm, swap, faults = psutil.virtual_memory(), psutil.swap_memory(), self.vmstats()
        fault_rate = max(0, faults.get('pgfault', 0) - self.old_faults.get('pgfault', 0)) / elapsed
        major_rate = max(0, faults.get('pgmajfault', 0) - self.old_faults.get('pgmajfault', 0)) / elapsed
        swap_in = max(0, swap.sin - self.old_swap.sin) / elapsed
        swap_out = max(0, swap.sout - self.old_swap.sout) / elapsed
        self.old_swap, self.old_faults = swap, faults
        rows, alive = [], set()
        for item in psutil.process_iter(['pid', 'name', 'username', 'memory_info', 'memory_percent', 'create_time']):
            try:
                info = item.info
                key = (item.pid, info['create_time'])
                process = self.processes.setdefault(key, item)
                alive.add(key)
                cpu = process.cpu_percent()
                rss = info['memory_info'].rss
                rows.append((info, cpu, rss))
            except (psutil.Error, AttributeError):
                continue
        self.processes = {key: value for key, value in self.processes.items() if key in alive}
        if self.frozen:
            return
        self.update_cpu_panel()
        self.query_one('#memory', Static).update(
            f'[bold cyan]MEMORY[/]  {size(vm.total)} installed\n{meter(vm.percent)}\n'
            f'Used {size(vm.used)}   Available {size(vm.available)}\n'
            f'Cache {size(getattr(vm, "cached", 0))}   Buffers {size(getattr(vm, "buffers", 0))}\n'
            f'Swap {size(swap.used)} / {size(swap.total)}')
        model = self.cpu_model
        self.query_one('#system', Static).update(Text(
            f'{model}\n{psutil.cpu_count(logical=False)} cores / {psutil.cpu_count()} threads\n'
            f'Faults {fault_rate:,.0f}/s · Major {major_rate:,.1f}/s\n'
            f'Swap I/O ↓ {size(swap_in)}/s ↑ {size(swap_out)}/s\n'
            f'Refresh {self.interval:g}s · Sort {self.sort_key} {"↓" if self.sort_reverse else "↑"}'))
        titles = {1: 'IMC CONTROLLER / CHANNEL COUNTER DOMAINS', 2: 'AGGREGATE IMC DRAM TRAFFIC',
                  3: 'RESCTRL MBM · ROOT GROUP LLC ↔ MEMORY', 4: 'CACHE EVENTS · DRAM BANDWIDTH N/A'}
        # Summarize generic counters instead of flooding the pane with one line per thread.
        if self.hardware.tier == 4:
            cache = cache_rates(samples)
            ratio = cache['miss ratio']
            lines = [titles[4] + f' · {len(samples)} opened counters',
                     'Generic cache miss/reference ratio: ' + (f'{ratio:.1f}%' if ratio is not None else 'N/A'),
                     'DRAM byte counters unavailable · D: discovery / access details']
        else:
            lines = [titles[self.hardware.tier]]
            for event, value in samples:
                formatted = 'N/A' if value is None else (size(value) + '/s' if event.unit == 'B/s' else f'{value:,.0f} {event.unit}')
                lines.append(f'{event.label}  {formatted}')
        if not samples:
            lines.append('No readable hardware counters. Press D for discovery and permission details.')
        self.query_one('#hardware', Static).update(Text('\n'.join(lines)))
        self.update_memory_performance(samples, vm)
        self.update_bandwidth(samples)
        self.update_agents(samples)
        self.history.append(vm.percent)
        self.query_one('#trend', Sparkline).data = list(self.history)
        mode = {1: 'IMC domains', 2: 'Aggregate IMC', 3: 'resctrl root-group MBM', 4: 'Cache / OS fallback'}[self.hardware.tier]
        self.query_one('#status', Static).update(
            f'{mode} · histories auto-scale independently · allocation history in %\n'
            'A: address samples · B: process samples · D: hardware details')
        self.process_rows = rows
        self.render_processes()

    def process_sort_value(self, row, key=None):
        info, cpu, rss = row
        return {'pid': info['pid'], 'username': (info['username'] or '').casefold(),
                'cpu': cpu, 'memory_info': rss, 'memory_percent': info['memory_percent'],
                'name': (info['name'] or '').casefold()}[key or self.sort_key]

    def render_processes(self):
        for cpu in (False, True):
            query = self.query_one('#cpu-search' if cpu else '#search', Input).value.casefold()
            key = self.cpu_sort_key if cpu else self.sort_key
            reverse = self.cpu_sort_reverse if cpu else self.sort_reverse
            rows = sorted(self.process_rows, key=lambda row: self.process_sort_value(row, key), reverse=reverse)
            table = self.query_one('#cpu-processes' if cpu else '#processes', DataTable)
            cursor = table.cursor_row
            table.clear()
            for info, usage, rss in rows:
                if query and query not in f'{info["pid"]} {info["name"]}'.casefold():
                    continue
                table.add_row(str(info['pid']), Text(info['username'] or '?'), f'{usage:.1f}', size(rss),
                              f'{info["memory_percent"]:.1f}', Text(info['name'] or '?'))
            if table.row_count:
                table.move_cursor(row=min(cursor, table.row_count-1), scroll=False)

    def update_memory_performance(self, samples, vm):
        tier = self.hardware.tier
        self.query_one('#memory-tab').set_class(tier not in (1, 2), 'no-dram')
        self.query_one('#agents').display = self.size.height >= 30 and any(event.tier == 5 for event, _ in samples)
        self.query_one('#cache-performance').display = tier == 4
        self.query_one('#mbm-performance').display = tier == 3
        cache, mbm = cache_rates(samples), mbm_rates(samples)
        cards = [('cache references', 'cache-references', 'CACHE REFERENCES', cache['cache references'], 'events/s'),
                 ('cache misses', 'cache-misses', 'CACHE MISSES', cache['cache misses'], 'events/s'),
                 ('mbm_total_bytes', 'mbm-total', 'LLC ↔ MEMORY · TOTAL', mbm['mbm_total_bytes'], 'B/s'),
                 ('mbm_local_bytes', 'mbm-local', 'LLC ↔ MEMORY · LOCAL', mbm['mbm_local_bytes'], 'B/s')]
        for key, selector, label, value, unit in cards:
            history = self.performance_histories[key]
            graph = self.query_one(f'#{selector}-graph', Sparkline)
            graph.display = value is not None
            if value is None:
                history.clear()
            else:
                history.append(value)
            graph.data = list(history)
            text = 'N/A · unavailable / warming up' if value is None else (
                size(value)+'/s' if unit == 'B/s' else f'{value:,.0f} events/s')
            if self.size.height < 30:
                compact = 'N/A' if value is None else (size(value)+'/s' if unit == 'B/s' else f'{value/1e6:.2f}M events/s')
                title = {'cache references': 'REF', 'cache misses': 'MISS', 'mbm_total_bytes': 'TOTAL', 'mbm_local_bytes': 'LOCAL'}[key]
                self.query_one(f'#{selector}-label', Static).update(Text(title+' '+compact))
            else:
                self.query_one(f'#{selector}-label', Static).update(Text(label+'\n'+text))
        psi = pressure()
        fields = memory_fields()
        def field_size(name):
            return size(fields[name]) if name in fields else "N/A"
        def stall(name):
            return f'{psi[name]:.2f}%' if name in psi else 'N/A'
        self.query_one('#memory-pressure', Static).update(Text(
            f'Memory stalls (10s avg): some {stall("some")} · full {stall("full")}\n'
            f'Dirty {field_size("Dirty")} · writeback {field_size("Writeback")} · reclaimable slab {field_size("SReclaimable")}'))

    def update_bandwidth(self, samples):
        rates = dram_rates(samples)
        for direction, value in rates.items():
            history = self.bandwidth_history[direction]
            graph = self.query_one(f'#{direction}-graph', Sparkline)
            label = self.query_one(f'#{direction}-label', Static)
            graph.display = value is not None
            if value is None:
                # Do not connect a line across unavailable intervals or graph missing data as zero.
                history.clear()
                graph.data = []
                label.update(f'[bold]{direction.upper()} · RAM[/]  N/A\n'
                             'No readable IMC byte counter · D: details')
            else:
                history.append(value)
                graph.data = list(history)
                color = 'green' if direction == 'read' else 'yellow'
                label.update(f'[bold {color}]{direction.upper()} · RAM[/]  {size(value)}/s\n'
                             f'Peak {size(max(history))}/s · {len(history)} samples')

    def update_agents(self, samples):
        for agent, title in [('ia', 'CPU · IA'), ('gt', 'GPU · GT'), ('io', 'I/O')]:
            values = [value for event, value in samples
                      if event.unit == 'requests/s' and event.label.endswith('/' + agent + '_requests')]
            value = sum(values) if values and all(v is not None for v in values) else None
            history = self.bandwidth_history[agent]
            graph = self.query_one(f'#{agent}-graph', Sparkline)
            graph.display = value is not None
            if value is None:
                history.clear()
                graph.data = []
                text = f'{title} · N/A\nIMC requests unavailable / warming up'
            else:
                history.append(value)
                graph.data = list(history)
                text = f'{title} · {value:,.0f} requests/s\nRead + write requests · not byte bandwidth'
            self.query_one(f'#{agent}-label', Static).update(Text(text))

    def on_tabbed_content_tab_activated(self, event: TabbedContent.TabActivated):
        if event.tabbed_content.id != 'main-tabs':
            return
        self.call_after_refresh(self.update_cpu_panel)
        if event.pane.id == 'gpu-tab':
            self.query_one(GpuView).refresh_gpu()
        elif event.pane.id == 'disk-tab':
            self.query_one(DiskPanel).refresh_io()
        elif event.pane.id == 'network-tab':
            self.query_one(NetworkPanel).refresh_io()
        elif event.pane.id == 'lan-tab':
            self.query_one(LanPanel).refresh_io()

    def action_hotspots(self):
        from .hotspots import Hotspots
        self.push_screen(Hotspots())

    def action_gpu(self):
        self.action_tab('gpu-tab')

    def action_tab(self, name):
        self.set_focus(None)
        self.query_one('#main-tabs', TabbedContent).active = name
        self.call_after_refresh(self.update_cpu_panel)
        self.call_after_refresh(self.focus_tab, name)

    def focus_tab(self, name):
        selector = {'memory-tab': '#processes', 'cpu-tab': '#cpu-processes',
                    'gpu-tab': '#gpu-engines', 'disk-tab': '#disk-devices',
                    'network-tab': '#network-interfaces', 'lan-tab': '#lan-nodes'}[name]
        self.query_one(selector).focus(scroll_visible=False)

    def action_process_samples(self):
        from .hotspots import Hotspots
        self.push_screen(Hotspots(process_view=True))

    def on_button_pressed(self, event: Button.Pressed):
        if event.button.id == 'process-samples':
            event.stop()
            self.action_process_samples()

    def action_toggle_cpu(self):
        tabs = self.query_one('#main-tabs', TabbedContent)
        self.action_tab('memory-tab' if tabs.active == 'cpu-tab' else 'cpu-tab')

    def update_cpu_panel(self):
        if not self.cpu_visible or not self.query('#cpu-panel'):
            return
        panel = self.query_one('#cpu-panel')
        self.query_one('#cpu-bars', Static).update(cpu_display(self.cpu_monitor.usages, max(30, panel.size.width - 4)))
        load = psutil.getloadavg()
        self.query_one('#cpu-title', Static).update(
            f'[bold cyan]CPU UTILIZATION[/] · {psutil.cpu_count(logical=False)} cores / '
            f'{len(self.cpu_monitor.usages)} logical CPUs · Load {load[0]:.2f} {load[1]:.2f} {load[2]:.2f}'
            + (' · FROZEN' if self.frozen else ''))

    def action_help(self): self.push_screen(Help())
    def action_diagnostics(self): self.push_screen(Diagnostics())
    def action_freeze(self):
        self.frozen = not self.frozen
        self.sub_title = 'FROZEN' if self.frozen else 'system performance'
    PROCESS_COLUMNS = ('pid', 'username', 'cpu', 'memory_info', 'memory_percent', 'name')
    PROCESS_LABELS = ('PID', 'USER', 'CPU %', 'RSS', 'MEM %', 'PROCESS · resident memory, not bandwidth')

    def on_data_table_header_selected(self, event):
        if event.data_table.id not in ('processes', 'cpu-processes'):
            return
        event.stop()
        cpu = event.data_table.id == 'cpu-processes'
        current, descending = (self.cpu_sort_key, self.cpu_sort_reverse) if cpu else (self.sort_key, self.sort_reverse)
        key = self.PROCESS_COLUMNS[event.column_index]
        reverse = not descending if key == current else key in ('cpu', 'memory_info', 'memory_percent')
        self.set_process_sort(key, reverse, cpu=cpu)

    def set_process_sort(self, key, reverse, cpu=False):
        if cpu:
            self.cpu_sort_key, self.cpu_sort_reverse = key, reverse
        else:
            self.sort_key, self.sort_reverse = key, reverse
        table = self.query_one('#cpu-processes' if cpu else '#processes', DataTable)
        for column, name, label in zip(table.ordered_columns, self.PROCESS_COLUMNS, self.PROCESS_LABELS):
            column.label = Text(label + (' ↓' if reverse else ' ↑') if name == key else label)
            column.content_width = max(column.content_width, len(column.label))
        table.refresh(layout=True)
        self.render_processes()

    def on_input_changed(self, event):
        if event.input.id in ('search', 'cpu-search') and self.query_one('#processes', DataTable).columns:
            self.render_processes()

    def action_sort_memory(self):
        self.set_process_sort('memory_info', True, cpu=self.query_one('#main-tabs', TabbedContent).active == 'cpu-tab')
    def action_sort_cpu(self):
        self.set_process_sort('cpu', True, cpu=self.query_one('#main-tabs', TabbedContent).active == 'cpu-tab')
    def action_filter(self):
        active = self.query_one('#main-tabs', TabbedContent).active
        selector = {'memory-tab': '#search', 'cpu-tab': '#cpu-search', 'disk-tab': '#disk-filter',
                    'network-tab': '#network-filter', 'lan-tab': '#lan-filter'}.get(active)
        if selector:
            self.query_one(selector, Input).focus()
    def action_clear_filter(self):
        active = self.query_one('#main-tabs', TabbedContent).active
        selector = {'memory-tab': '#search', 'cpu-tab': '#cpu-search', 'disk-tab': '#disk-filter',
                    'network-tab': '#network-filter', 'lan-tab': '#lan-filter'}.get(active)
        if selector:
            self.query_one(selector, Input).value = ''
        if active in ('memory-tab', 'cpu-tab'):
            self.query_one('#cpu-processes' if active == 'cpu-tab' else '#processes', DataTable).focus()

    def change_interval(self, factor):
        self.interval = round(min(10, max(.25, self.interval * factor)), 2)
        self.timer.stop()
        self.timer = self.set_interval(self.interval, self.tick)
    def action_faster(self): self.change_interval(.5)
    def action_slower(self): self.change_interval(2)

    def on_unmount(self):
        if hasattr(self, "timer"):
            self.timer.stop()
