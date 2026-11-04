import asyncio
from rich.text import Text
from textual import work
from textual.widgets import Button, Input, Select, Static
from .io_views import IoPanel, graphs
from .lan import LanMonitor, discover_nodes
from .metric_table import MetricTable


class LanPanel(IoPanel):
    kind = 'lan'

    def make_monitor(self):
        monitor = getattr(self, 'monitor', None)
        if monitor is not None:
            monitor.reset()
            return monitor
        return LanMonitor()

    def compose(self):
        yield Select([], prompt='Choose LAN interface', id='lan-device')
        yield from graphs('lan')
        yield Static('Discovered local addresses · TX → node / RX ← node, from this laptop’s perspective.\n'
                     'TCP payload rates only; not each node’s total traffic. One device can have multiple addresses.', classes='io-note')
        yield Button('Discover nodes on this interface', id='lan-discover', variant='primary')
        yield Static('Neighbor cache inventory · click Discover for bounded IPv4 probes and mDNS names.', id='lan-discovery', classes='io-note')
        yield Input(placeholder='Filter IP, name, MAC or neighbor state', id='lan-filter')
        yield MetricTable(list(zip(('INTERFACE', 'IP ADDRESS', 'NAME', 'MAC', 'NEIGHBOR STATE',
                                    'TX → NODE/s', 'RX ← NODE/s', 'TCP FLOWS', 'DISCOVERY'),
                                   ('text', 'text', 'text', 'text', 'text', 'rate', 'rate', 'int', 'text'))),
                          id='lan-nodes', sort=6, reverse=True)
        yield Static(id='lan-note', classes='io-note')

    def render_snapshot(self, append=True):
        from .app import size
        data = self.snapshot
        self.update_graphs(data['interfaces'], append)
        selector = self.query_one('#lan-device', Select)
        selected = selector.value
        chosen = next((row for row in data['interfaces'] if row[0] == selected), None)
        for index, title in enumerate(('RX ← LAN NODES', 'TX → LAN NODES')):
            value = chosen[index+1] if chosen else None
            self.query_one(f'#lan-label-{index}', Static).update(Text(
                f'{title} · observed TCP payload\n' + (size(value)+'/s' if value is not None else 'N/A · no complete rate baseline')))
        query = self.query_one('#lan-filter', Input).value.casefold()
        rows = [row for row in data['nodes'] if row[0] == selected and query in ' '.join(str(v) for v in row).casefold()]
        self.query_one('#lan-nodes', MetricTable).set_records(rows)
        self.query_one('#lan-note', Static).update(Text(
            (data['error']+'\n' if data['error'] else '')
            + f'{len(rows)} known addresses on {selected if chosen else "available interfaces"}. '
            'STALE/cache entries do not prove a device is online.\n'
            'N/A means no measured TCP baseline; UDP, short-lived flows and other machines’ traffic are outside this view.\n'
            'Discovery cannot guarantee every LAN device; silent/offline hosts and isolated segments may be absent.'))

    def on_button_pressed(self, event):
        if event.button.id == 'lan-discover':
            event.stop()
            self.discover()

    @work
    async def discover(self):
        if getattr(self, 'discovering', False) or not self.snapshot:
            return
        interface = self.query_one('#lan-device', Select).value
        if interface not in self.snapshot['inventory']:
            return
        self.discovering = True
        button = self.query_one('#lan-discover', Button)
        button.disabled = True
        self.query_one('#lan-discovery', Static).update(Text(f'Discovering on {interface}… up to 512 IPv4 targets; UI remains responsive.'))
        try:
            nodes, message = await asyncio.to_thread(discover_nodes, interface, self.snapshot['inventory'][interface])
            self.monitor.remember(interface, nodes)
            if self.is_mounted:
                self.query_one('#lan-discovery', Static).update(Text(f'{interface}: {message}'))
                self.refresh_io()
        finally:
            self.discovering = False
            if self.is_mounted:
                button.disabled = False
