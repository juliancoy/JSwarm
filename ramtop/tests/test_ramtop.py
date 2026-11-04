import asyncio
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch

from ramtop.hardware import Counter, Event, Hardware, MBMCounter, discover, encode


class HardwareTests(unittest.TestCase):
    def test_sparse_format_and_mib_scale(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pmu = root / 'uncore_imc'
            (pmu / 'events').mkdir(parents=True)
            (pmu / 'format').mkdir()
            for path, content in {'type': '12', 'cpumask': '0,4', 'format/event': 'config:0-3,8-11',
                                  'events/data_reads': 'event=0xab', 'events/data_reads.scale': '0.00006103515625',
                                  'events/data_reads.unit': 'MiB'}.items():
                (pmu / path).write_text(content)
            self.assertEqual(encode(pmu, 'event=0xab')['config'], 0xa0b)
            events, notes = discover(root)
            self.assertEqual(len(events), 2)
            self.assertEqual(events[0].scale, 64)
            self.assertEqual(events[0].tier, 2)
            (pmu / 'events/data_reads.unit').unlink()
            self.assertEqual(discover(root)[0], [])

    def test_interval_multiplex_scaling_and_unscheduled(self):
        counter = Counter.__new__(Counter)
        counter.fd, counter.previous = 1, None
        counter.event = Event('test', 1, {}, 0, 64)
        with patch('os.read', side_effect=[struct.pack('QQQ', *x) for x in
                                         [(100, 100, 50), (200, 300, 150), (200, 400, 150), (1, 1, 1)]]):
            self.assertIsNone(counter.sample(1))
            self.assertEqual(counter.sample(2), 6400)
            self.assertIsNone(counter.sample(1))
            self.assertIsNone(counter.sample(1))

    def test_mbm_invalid_and_reset(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'mbm_total_bytes'
            counter = MBMCounter(path)
            outputs = []
            for raw in ('100', '300', 'Unavailable', '400', '10'):
                path.write_text(raw)
                outputs.append(counter.sample(2))
            self.assertEqual(outputs, [None, 100, None, None, None])

    def test_permission_fallback(self):
        with tempfile.TemporaryDirectory() as directory, patch('ramtop.hardware.Counter', side_effect=PermissionError(13, 'denied')):
            hardware = Hardware(Path(directory), Path(directory))
            self.assertEqual(hardware.tier, 4)
            self.assertEqual(hardware.sample(1), [])
            self.assertTrue(hardware.notes)
            hardware.close()


class UITests(unittest.TestCase):
    def test_controls_and_small_screen(self):
        from ramtop.app import Ramtop
        from textual.widgets import Input
        class FakeHardware:
            tier, notes, inventory = 4, ['test permission failure'], {}
            def sample(self, elapsed): return []
        async def exercise():
            app = Ramtop(FakeHardware())
            async with app.run_test(size=(100, 35)) as pilot:
                await pilot.pause()
                await pilot.press('escape', 'space')
                self.assertTrue(app.frozen)
                await pilot.press('space', 'p', '+')
                self.assertFalse(app.frozen)
                self.assertEqual(app.sort_key, 'cpu')
                self.assertEqual(app.interval, .5)
                await pilot.press('/')
                self.assertIsInstance(app.focused, Input)
                await pilot.press('escape', 'f1')
                self.assertEqual(type(app.screen).__name__, 'Help')
                await pilot.press('escape', 'd')
                self.assertEqual(type(app.screen).__name__, 'Diagnostics')
                await pilot.press('escape')
                await pilot.resize_terminal(60, 22)
                await pilot.pause()
                with tempfile.TemporaryDirectory() as screenshot_dir:
                    app.save_screenshot('preview.svg', path=screenshot_dir)
        asyncio.run(exercise())


class BandwidthTests(unittest.TestCase):
    def test_directional_sums_and_unknown_samples(self):
        from ramtop.app import dram_rates
        def event(alias, tier=2):
            return Event('imc/' + alias, 1, {}, 0, unit='B/s', tier=tier)
        samples = [(event('data_reads'), 100), (event('data_read'), 200),
                   (event('data_writes'), 50), (event('mbm_total_bytes', 3), 999)]
        self.assertEqual(dram_rates(samples), {'read': 300, 'write': 50})
        samples.append((event('data_reads'), None))
        self.assertEqual(dram_rates(samples), {'read': None, 'write': 50})
        self.assertEqual(dram_rates([]), {'read': None, 'write': None})

    def test_graphs_show_separate_values_and_hide_missing(self):
        from ramtop.app import Ramtop
        from textual.widgets import Sparkline
        class FakeHardware:
            tier, notes, inventory = 2, [], {}
            def sample(self, elapsed):
                return [(Event('imc/data_reads', 1, {}, 0, unit='B/s', tier=2), 1024),
                        (Event('imc/data_writes', 1, {}, 0, unit='B/s', tier=2), 2048)]
        async def exercise():
            app = Ramtop(FakeHardware())
            async with app.run_test(size=(110, 42)) as pilot:
                await pilot.pause()
                self.assertEqual(list(app.query_one('#read-graph', Sparkline).data)[-1], 1024)
                self.assertEqual(list(app.query_one('#write-graph', Sparkline).data)[-1], 2048)
                app.update_bandwidth([])
                self.assertFalse(app.query_one('#read-graph', Sparkline).display)
                self.assertEqual(list(app.bandwidth_history['write']), [])
        asyncio.run(exercise())


class SamplingTests(unittest.TestCase):
    def test_address_ranges_keep_processes_separate_and_ignore_unknowns(self):
        from ramtop.sampling import aggregate
        rows, ignored = aggregate([
            'worker name 12 cpu/mem-loads,ldlat=30/P: 1001 90',
            'worker name 12 cpu/mem-stores/P: 1fff 0',
            'worker name 13 cpu/mem-loads,ldlat=30/P: 1001 60',
            'worker name 12 cpu/mem-loads,ldlat=30/P: 0 70',
            'invalid line',
        ])
        self.assertEqual(len(rows), 2)
        self.assertEqual((rows[0].pid, rows[0].address, rows[0].loads, rows[0].stores), (12, 4096, 1, 1))
        self.assertEqual(rows[0].load_weight, 90)
        self.assertEqual(ignored, 2)

    def test_request_discovery_does_not_use_misleading_byte_scale(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pmu = root / 'uncore_imc'
            (pmu / 'events').mkdir(parents=True)
            (pmu / 'format').mkdir()
            for path, content in {'type': '12', 'cpumask': '0', 'format/event': 'config:0-7',
                                  'events/ia_requests': 'event=0x04', 'events/ia_requests.scale': '0.00006103515625',
                                  'events/ia_requests.unit': 'MiB'}.items():
                (pmu / path).write_text(content)
            events, _ = discover(root)
            self.assertEqual((events[0].scale, events[0].unit, events[0].tier), (1, 'requests/s', 5))

    def test_hotspot_modal_capture_and_error(self):
        from ramtop.app import Ramtop
        from ramtop.sampling import Hotspot
        from textual.widgets import DataTable, Static
        class FakeHardware:
            tier, notes, inventory = 4, [], {}
            def sample(self, elapsed): return []
        async def exercise():
            app = Ramtop(FakeHardware())
            with patch('ramtop.hotspots.capture', return_value=([Hotspot(42, 'worker', 4096, 3, 1, 150)], 0, 'capture diagnostics')):
                async with app.run_test(size=(110, 42)) as pilot:
                    await pilot.press('a')
                    await pilot.pause(.2)
                    self.assertEqual(type(app.screen).__name__, 'Hotspots')
                    self.assertEqual(app.screen.query_one(DataTable).row_count, 1)
                    with patch('ramtop.hotspots.capture', side_effect=RuntimeError('permission denied')):
                        await pilot.press('r')
                        await pilot.pause(.2)
                        self.assertIn('permission denied', str(app.screen.query_one('#sample-diagnostics', Static).render()))
                    await pilot.press('escape')
        asyncio.run(exercise())


class HeaderSortingTests(unittest.TestCase):
    def test_process_header_mouse_sort_is_numeric_and_reversible(self):
        from ramtop.app import Ramtop
        from textual.widgets import DataTable
        class FakeHardware:
            tier, notes, inventory = 4, [], {}
            def sample(self, elapsed): return []
        async def exercise():
            app = Ramtop(FakeHardware())
            async with app.run_test(size=(140, 55)) as pilot:
                app.frozen = True
                app.process_rows = [({'pid': pid, 'username': user, 'name': user,
                                      'memory_percent': 1.0}, cpu, rss)
                                    for pid, user, cpu, rss in [(10, 'alice', 2., 1024), (2, 'bob', 10., 512)]]
                app.render_processes()
                await pilot.pause()
                await pilot.click('#processes', offset=(2, 0))
                table = app.query_one('#processes', DataTable)
                self.assertEqual(table.get_row_at(0)[0], '2')
                self.assertEqual(app.sort_key, 'pid')
                self.assertFalse(app.sort_reverse)
                await pilot.click('#processes', offset=(2, 0))
                self.assertEqual(table.get_row_at(0)[0], '10')
                app.action_sort_cpu()
                self.assertEqual(table.get_row_at(0)[0], '2')
                self.assertEqual(app.sort_key, 'cpu')
        asyncio.run(exercise())

    def test_hotspot_headers_sort_numeric_addresses_counts_and_latency(self):
        from ramtop.app import Ramtop
        from ramtop.sampling import Hotspot
        from textual.widgets import DataTable
        class FakeHardware:
            tier, notes, inventory = 4, [], {}
            def sample(self, elapsed): return []
        async def exercise():
            app = Ramtop(FakeHardware())
            rows = [Hotspot(10, 'a', 0x10000, 2, 10, 200), Hotspot(2, 'b', 0x2000, 10, 2, 300),
                    Hotspot(3, 'c', 0x3000, 0, 3, 0)]
            with patch('ramtop.hotspots.capture', return_value=(rows, 0, '')):
                async with app.run_test(size=(140, 55)) as pilot:
                    await pilot.press('a')
                    await pilot.pause(.2)
                    screen = app.screen
                    table = screen.query_one(DataTable)
                    await pilot.click('#sample-table', offset=(2, 0))
                    self.assertEqual(table.get_row_at(0)[0], '2')
                    await pilot.click('#sample-table', offset=(2, 0))
                    self.assertEqual(table.get_row_at(0)[0], '10')
                    screen.sort_column, screen.sort_reverse = 5, False
                    screen.render_rows()
                    self.assertEqual(table.get_row_at(0)[0], '2')
                    self.assertEqual(table.get_row_at(2)[0], '3')
        asyncio.run(exercise())


class CpuUtilizationTests(unittest.TestCase):
    def test_cpu_percentages_exclude_guest_double_counting_and_wait(self):
        from collections import namedtuple
        from ramtop.cpu import utilization
        Times = namedtuple('Times', 'user nice system idle iowait irq softirq steal guest guest_nice')
        before = Times(*([0] * 10))
        after = Times(40, 10, 20, 10, 10, 3, 2, 5, 15, 5)
        usage = utilization(before, after)
        self.assertEqual(usage.user, 40)
        self.assertEqual(usage.system, 25)
        self.assertEqual(usage.wait, 10)
        self.assertEqual(usage.busy, 80)
        self.assertIsNone(utilization(after, after))
        self.assertIsNone(utilization(after, before))

    def test_cpu_button_and_keyboard_toggle_live_panel(self):
        from ramtop.app import Ramtop
        from ramtop.cpu import CpuUsage
        from textual.widgets import Static
        class FakeHardware:
            tier, notes, inventory = 4, [], {}
            def sample(self, elapsed): return []
        async def exercise():
            app = Ramtop(FakeHardware())
            async with app.run_test(size=(120, 55)) as pilot:
                self.assertEqual(app.query_one('#main-tabs').active, 'memory-tab')
                app.cpu_monitor.usages = [CpuUsage(20, 10, 0, 0, 5)] * 8
                app.cpu_monitor.sample = lambda: app.cpu_monitor.usages
                await pilot.press('c')
                await pilot.pause()
                self.assertTrue(app.cpu_visible)
                self.assertTrue(app.query_one('#cpu-panel').display)
                rendered = app.query_one('#cpu-bars', Static).content
                self.assertEqual(rendered.row_count, 5)
                # Move focus from button to allow the keyboard shortcut.
                app.query_one('#cpu-processes').focus()
                await pilot.press('c')
                self.assertEqual(app.query_one('#main-tabs').active, 'memory-tab')
                await pilot.press('c')
                await pilot.resize_terminal(60, 30)
                app.update_cpu_panel()
                self.assertTrue(app.query_one('#cpu-panel').display)
                self.assertEqual(app.query_one('#cpu-bars', Static).content.row_count, 9)
        asyncio.run(exercise())


class ProcessSampleTests(unittest.TestCase):
    def test_summary_includes_all_ranges_and_weights_latency(self):
        from ramtop.sampling import Hotspot, summarize_processes
        rows = [Hotspot(12, 'worker', i * 4096, 1, 2, 30) for i in range(600)]
        rows += [Hotspot(12, 'thread', 0, 2, 1, 120), Hotspot(13, 'worker', 0, 0, 7, 0)]
        summary = {row.pid: row for row in summarize_processes(rows)}
        self.assertEqual((summary[12].loads, summary[12].stores, summary[12].ranges), (602, 1201, 600))
        self.assertEqual(summary[12].load_weight, 18120)
        self.assertEqual(summary[12].command, 'thread, worker')
        self.assertEqual(summary[13].stores, 7)

    def test_process_button_and_store_header_sort(self):
        from ramtop.app import Ramtop
        from ramtop.sampling import Hotspot
        from textual.widgets import DataTable
        class FakeHardware:
            tier, notes, inventory = 4, [], {}
            def sample(self, elapsed): return []
        async def exercise():
            app = Ramtop(FakeHardware())
            rows = [Hotspot(10, 'a', 4096, 1, 20, 60), Hotspot(2, 'b', 4096, 10, 2, 300)]
            with patch('ramtop.hotspots.capture', return_value=(rows, 0, '')):
                async with app.run_test(size=(140, 55)) as pilot:
                    await pilot.click('#process-samples')
                    await pilot.pause(.2)
                    screen = app.screen
                    self.assertTrue(screen.process_view)
                    table = screen.query_one(DataTable)
                    self.assertEqual(table.get_row_at(0)[0], '2')
                    x = sum(column.get_render_width(table) for column in table.ordered_columns[:3]) + 1
                    await pilot.click('#sample-table', offset=(x, 0))
                    self.assertEqual(table.get_row_at(0)[0], '10')
                    await pilot.click('#sample-table', offset=(x, 0))
                    self.assertEqual(table.get_row_at(0)[0], '2')
                    await pilot.press('s')
                    self.assertFalse(screen.process_view)
                    await pilot.press('s')
                    self.assertEqual(screen.sort_column, 3)
                    self.assertFalse(screen.sort_reverse)
                    await pilot.press('r')
                    await pilot.pause(.2)
                    self.assertEqual(table.get_row_at(0)[0], '2')
        asyncio.run(exercise())


class GpuTests(unittest.TestCase):
    def test_fdinfo_memory_units_and_duplicate_clients(self):
        from ramtop.gpu import clients, parse_fdinfo
        text = ('drm-driver: i915\ndrm-client-id: 4\ndrm-pdev: 0000:00:02.0\n'
                'drm-total-system0: 12 KiB\ndrm-resident-system0: 2 MiB\n'
                'drm-engine-render: 500 ns\n')
        key, metrics = parse_fdinfo(text)
        self.assertEqual(key, ('0000:00:02.0', '4'))
        self.assertEqual(metrics['drm-total-system0'], 12288)
        self.assertEqual(metrics['drm-resident-system0'], 2097152)
        self.assertIsNone(parse_fdinfo('drm-driver: amdgpu\n'))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for pid in (10, 20):
                process = root / str(pid)
                (process / 'fdinfo').mkdir(parents=True)
                (process / 'fd').mkdir()
                (process / 'comm').write_text(f'proc{pid}')
                for fd in ('5', '9'):
                    (process / 'fdinfo' / fd).write_text(text)
                    (process / 'fd' / fd).symlink_to('/dev/dri/renderD128')
                (process / 'fd' / '99').symlink_to('socket:[123]')
                # A socket fdinfo must never be opened as a GPU candidate.
                (process / 'fdinfo' / '99').mkdir()
            rows, denied = clients(root)
            self.assertEqual(len(rows), 1)
            self.assertEqual(len(rows[0]['owners']), 2)
            self.assertEqual(denied, 0)

    def test_gpu_button_tabs_unavailable_bandwidth_and_client_sort(self):
        from ramtop.app import Ramtop
        from textual.widgets import DataTable, TabbedContent
        class FakeHardware:
            tier, notes, inventory = 4, [], {}
            def sample(self, elapsed): return []
        class FakeGpu:
            def sample(self):
                return dict(rates={'rcs0-busy': (25., '%')}, denied=0, sensors=[('frequency', '300 MHz')], notes=[],
                            clients=[{'owners': {(10,'a')}, 'key': ('pci', '1'),
                                      'metrics': {'drm-resident-system0': 1000}, 'engines': {'render': 25.}},
                                     {'owners': {(2,'b')}, 'key': ('pci', '2'),
                                      'metrics': {'drm-resident-system0': 9000}, 'engines': {'render': 2.}}])
            def close(self): pass
        async def exercise():
            app = Ramtop(FakeHardware())
            with patch('ramtop.gpu_view.GpuMonitor', return_value=FakeGpu()):
                async with app.run_test(size=(140, 55)) as pilot:
                    await pilot.press('g')
                    await pilot.pause(.5)
                    screen = app.query_one('GpuView')
                    self.assertEqual(app.query_one('#main-tabs').active, 'gpu-tab')
                    self.assertFalse(screen.query_one('#gpu-read-graph').display)
                    self.assertFalse(screen.query_one('#gpu-write-graph').display)
                    self.assertEqual(screen.query_one('#gpu-engines', DataTable).row_count, 1)
                    screen.query_one('#gpu-tabs', TabbedContent).active = 'gpu-client-tab'
                    await pilot.pause()
                    table = screen.query_one('#gpu-clients', DataTable)
                    self.assertEqual(str(table.get_row_at(0)[0]), '2 b')
                    x = sum(c.get_render_width(table) for c in table.ordered_columns[:3]) + 1
                    await pilot.click('#gpu-clients', offset=(x, 0))
                    self.assertEqual(str(table.get_row_at(0)[0]), '10 a')
                    await pilot.press('escape')
                    self.assertEqual(type(app.screen).__name__, 'Screen')
        asyncio.run(exercise())


class DiskNetworkTests(unittest.TestCase):
    def test_device_rates_latency_and_counter_resets(self):
        from collections import namedtuple
        from ramtop.io_metrics import DiskMonitor, rate
        Stats = namedtuple('Stats', 'read_count write_count read_bytes write_bytes read_time write_time busy_time')
        before = Stats(1, 2, 1000, 1000, 10, 20, 100)
        after = Stats(3, 6, 2000, 5000, 50, 100, 2100)
        with patch('ramtop.io_metrics.time.monotonic', side_effect=[0, 1, 3]), \
             patch('psutil.disk_io_counters', side_effect=[{'fake0': before}, {'fake0': after}]), \
             patch('psutil.process_iter', return_value=[]), patch('psutil.disk_partitions', return_value=[]):
            monitor = DiskMonitor()
            self.assertIsNone(monitor.sample()['devices'][0][1])
            row = monitor.sample()['devices'][0]
            self.assertEqual(row[1:8], (500., 2000., 1., 2., 20., 20., 100.))
        self.assertIsNone(rate(1, 10, 2))
        self.assertIsNone(rate(10, None, 2))
        self.assertIsNone(rate(10, 1, 0))

    def test_socket_payload_rates_and_identity(self):
        from ramtop.io_metrics import NetworkMonitor, parse_ss
        from types import SimpleNamespace
        base = 'tcp ESTAB 0 5 [::1]:1234 [::1]:80 users:(("worker",pid=10,fd=4)) ino:12 sk:abc rtt:1.5/1 bytes_acked:{} bytes_received:{}'
        first = base.format(100, 200) + '\nudp ESTAB 0 0 1.2.3.4:1 1.2.3.5:2 ino:13 sk:aaa'
        second = base.format(300, 1000)
        parsed = parse_ss(first)
        self.assertEqual(parsed[0]['owners'], [(10, 'worker')])
        self.assertEqual(parsed[0]['local'], '[::1]:1234')
        self.assertIsNone(parsed[1]['tx'])
        with patch('ramtop.io_metrics.time.monotonic', side_effect=[0, 1, 3]), \
             patch('psutil.net_io_counters', return_value={}), patch('psutil.net_if_stats', return_value={}), \
             patch('ramtop.io_metrics.subprocess.run', side_effect=[SimpleNamespace(returncode=0, stdout=t, stderr='') for t in (first, second)]):
            monitor = NetworkMonitor()
            self.assertIsNone(monitor.sample()['connections'][0][3])
            row = monitor.sample()['connections'][0]
            self.assertEqual(row[3:6], (400., 100., 1.5))

    def test_five_tabs_io_graphs_filtering_and_header_sort(self):
        from ramtop.app import Ramtop
        from ramtop.metric_table import MetricTable
        from textual.widgets import Sparkline, Input
        class FakeHardware:
            tier, notes, inventory = 4, [], {}
            def sample(self, elapsed): return []
        class FakeDisk:
            def sample(self):
                return dict(devices=[('fake0', 1024., 2048., 1., 2., 3., 4., 50., 'device')],
                            processes=[(10, 'user', 'writer', 10., 20., 30., 40., 0),
                                       (2, 'user', 'reader', 20., 10., 40., 30., 0)], mounts=[], denied=0)
        class FakeNetwork:
            def sample(self):
                return dict(interfaces=[('eth0', 100., 200., 1., 2., 0., 0., 0., 0., 'up', 1000, 1500)],
                            connections=[('tcp', '127.0.0.1:10', '127.0.0.1:20', 20., 30., 1., 0, 0, 'ESTAB', '10 worker')], error='')
        async def exercise():
            app = Ramtop(FakeHardware())
            with patch('ramtop.io_views.DiskMonitor', FakeDisk), patch('ramtop.io_views.NetworkMonitor', FakeNetwork):
                async with app.run_test(size=(150, 55)) as pilot:
                    self.assertEqual([p.id for p in app.query_one('#main-tabs').query('TabPane') if p.id in
                                      ('memory-tab', 'cpu-tab', 'gpu-tab', 'disk-tab', 'network-tab')],
                                     ['memory-tab', 'cpu-tab', 'gpu-tab', 'disk-tab', 'network-tab'])
                    await pilot.press('4')
                    await pilot.pause(.4)
                    self.assertEqual(app.query_one('#main-tabs').active, 'disk-tab')
                    self.assertEqual(list(app.query_one('#disk-graph-0', Sparkline).data)[-1], 1024.)
                    disk = app.query_one('DiskPanel')
                    content = disk.query_one('TabbedContent')
                    content.active = list(content.query('TabPane'))[1].id
                    await pilot.pause()
                    table = app.query_one('#disk-processes', MetricTable)
                    self.assertEqual(str(table.get_row_at(0)[0]), '10')
                    x = sum(c.get_render_width(table) for c in table.ordered_columns[:4])+1
                    await pilot.click('#disk-processes', offset=(x,0))
                    self.assertEqual(str(table.get_row_at(0)[0]), '2')
                    app.query_one('#disk-filter', Input).value = 'writer'
                    await pilot.pause()
                    self.assertEqual(table.row_count, 1)
                    app.query_one('#disk-devices').focus()
                    await pilot.press('5')
                    await pilot.pause(.4)
                    self.assertEqual(app.query_one('#main-tabs').active, 'network-tab')
                    self.assertEqual(list(app.query_one('#network-graph-0', Sparkline).data)[-1], 100.)
                    self.assertEqual(app.query_one('#network-connections', MetricTable).row_count, 1)
                    await pilot.resize_terminal(65, 28)
                    await pilot.pause()
                    await pilot.press('1')
                    self.assertEqual(app.query_one('#main-tabs').active, 'memory-tab')
        asyncio.run(exercise())


class IoFallbackTests(unittest.TestCase):
    def test_disk_pid_reuse_does_not_inherit_rates(self):
        from types import SimpleNamespace
        from ramtop.io_metrics import DiskMonitor
        def io(value):
            return SimpleNamespace(read_bytes=value, write_bytes=value, read_chars=value, write_chars=value)
        first = SimpleNamespace(pid=99999991, info={'pid': 99999991, 'name': 'first', 'username': 'user', 'create_time': 1},
                                io_counters=unittest.mock.Mock(side_effect=[io(100), io(200)]))
        reused = SimpleNamespace(pid=99999991, info={'pid': 99999991, 'name': 'new', 'username': 'user', 'create_time': 2},
                                 io_counters=lambda: io(300))
        with patch('ramtop.io_metrics.time.monotonic', side_effect=[0, 1, 2, 3]), \
             patch('psutil.disk_io_counters', return_value={}), patch('psutil.disk_partitions', return_value=[]), \
             patch('psutil.process_iter', side_effect=[[first], [first], [reused]]):
            monitor = DiskMonitor()
            self.assertIsNone(monitor.sample()['processes'][0][3])
            self.assertEqual(monitor.sample()['processes'][0][3], 100.)
            self.assertIsNone(monitor.sample()['processes'][0][3])

    def test_ss_missing_retains_interface_rates(self):
        from types import SimpleNamespace
        from ramtop.io_metrics import NetworkMonitor
        stats = SimpleNamespace(bytes_recv=100, bytes_sent=200, packets_recv=1, packets_sent=2,
                                errin=0, errout=0, dropin=0, dropout=0)
        with patch('psutil.net_io_counters', return_value={'eth0': stats}), \
             patch('psutil.net_if_stats', return_value={}), \
             patch('ramtop.io_metrics.subprocess.run', side_effect=FileNotFoundError('ss unavailable')):
            result = NetworkMonitor().sample()
            self.assertEqual(result['interfaces'][0][0], 'eth0')
            self.assertEqual(result['connections'], [])
            self.assertIn('ss unavailable', result['error'])


class LanTests(unittest.TestCase):
    def test_discovery_parsing_and_large_subnet_limit(self):
        from ramtop.lan import interfaces_from_json, mdns_nodes, probe_targets, endpoint_address
        inventory = interfaces_from_json([{'ifname': 'eth0', 'operstate': 'UP', 'address': 'aa:bb',
                                          'addr_info': [{'local': '10.0.0.1', 'prefixlen': 30}]}])
        targets, skipped = probe_targets(inventory['eth0'])
        self.assertEqual(targets, ['10.0.0.2'])
        self.assertEqual(skipped, [])
        inventory['eth0']['networks'] = [__import__('ipaddress').ip_network('10.0.0.0/16')]
        self.assertEqual(probe_targets(inventory['eth0'])[0], [])
        self.assertEqual(endpoint_address('[fe80::1%eth0]:443'), 'fe80::1')
        self.assertEqual(mdns_nodes('=;eth0;IPv4;node;_ssh._tcp;local;node.local;10.0.0.2;22;', 'eth0'),
                         {'10.0.0.2': 'node.local'})

    def test_neighbors_are_not_gateway_internet_traffic_and_zeroes_are_not_invented(self):
        from ramtop.lan import LanMonitor
        class FakeNetwork:
            def sample(self):
                return dict(interfaces=[('eth0', 10000, 20000, 1, 1, 0, 0, 0, 0, 'up', 1000, 1500)], error='',
                            connections=[('tcp','10.0.0.1:10','10.0.0.2:20',100.,200.,1.,0,0,'ESTAB','1 worker'),
                                         ('tcp','10.0.0.1:11','8.8.8.8:443',999.,999.,1.,0,0,'ESTAB','1 worker')])
        addr = [{'ifname':'eth0','operstate':'UP','address':'aa:bb',
                 'addr_info':[{'local':'10.0.0.1','prefixlen':24}]}]
        neighbors = [{'dev':'eth0','dst':'10.0.0.2','lladdr':'cc:dd','state':['REACHABLE']},
                     {'dev':'eth0','dst':'10.0.0.3','lladdr':'ee:ff','state':['STALE']},
                     {'dev':'eth0','dst':'10.0.0.4','state':['FAILED']}]
        with patch('ramtop.lan.NetworkMonitor', FakeNetwork), patch('ramtop.lan.ip_json', side_effect=[addr,neighbors]):
            monitor=LanMonitor()
            result=monitor.sample()
        rows={row[1]:row for row in result['nodes']}
        self.assertEqual(rows['10.0.0.2'][5:8], (200.,100.,1))
        self.assertIsNone(rows['10.0.0.3'][5])
        self.assertNotIn('10.0.0.4',rows)
        self.assertNotIn('8.8.8.8',rows)
        self.assertEqual(result['interfaces'][0][1:3], (100.,200.))

    def test_lan_tab_graphs_filter_and_discovery_button(self):
        from ramtop.app import Ramtop
        from textual.widgets import Input
        from ramtop.metric_table import MetricTable
        import ipaddress
        class FakeHardware:
            tier, notes, inventory = 4, [], {}
            def sample(self, elapsed): return []
        class FakeLan:
            def reset(self): pass
            def remember(self, interface, nodes): self.remembered=(interface,nodes)
            def sample(self):
                return dict(interfaces=[('eth0',100.,200.,1,1,0,0,0,0,'up',1000,1500)], error='',
                            nodes=[('eth0','10.0.0.2','node.local','aa:bb','REACHABLE',200.,100.,1,'neighbor cache')],
                            inventory={'eth0':dict(addresses=['10.0.0.1'],networks=[ipaddress.ip_network('10.0.0.0/24')])})
        async def exercise():
            app=Ramtop(FakeHardware())
            with patch('ramtop.lan_view.LanMonitor',FakeLan), \
                 patch('ramtop.lan_view.discover_nodes',return_value=({'10.0.0.2':'node.local'},'1 result')) as discover:
                async with app.run_test(size=(145,55)) as pilot:
                    await pilot.press('6')
                    await pilot.pause(.4)
                    self.assertEqual(app.query_one('#main-tabs').active,'lan-tab')
                    self.assertEqual(app.query_one('#lan-nodes',MetricTable).row_count,1)
                    await pilot.click('#lan-discover')
                    await pilot.pause(.2)
                    discover.assert_called_once()
                    app.query_one('#lan-filter',Input).value='missing'
                    await pilot.pause()
                    self.assertEqual(app.query_one('#lan-nodes',MetricTable).row_count,0)
                    await pilot.press('escape')
                    self.assertEqual(app.query_one('#lan-nodes',MetricTable).row_count,1)
        asyncio.run(exercise())


class LanDiscoveryCompatibilityTests(unittest.TestCase):
    def test_avahi_command_and_interface_filter(self):
        from types import SimpleNamespace
        import ipaddress
        from ramtop.lan import discover_nodes
        commands=[]
        def command(args, **kwargs):
            commands.append(args)
            if args[0]=='ping':
                return SimpleNamespace(returncode=0)
            return SimpleNamespace(returncode=0,stderr='',stdout=(
                '=;eth0;IPv4;node;_ssh._tcp;local;node.local;10.0.0.2;22;\n'
                '=;wlan0;IPv4;other;_ssh._tcp;local;other.local;10.1.0.2;22;'))
        with patch('ramtop.lan.subprocess.run',side_effect=command), patch('ramtop.lan.shutil.which',return_value='/usr/bin/ping'):
            nodes,message=discover_nodes('eth0',dict(addresses=['10.0.0.1'],networks=[ipaddress.ip_network('10.0.0.0/30')]))
        self.assertEqual(nodes,{'10.0.0.2':'node.local'})
        self.assertNotIn('-i',commands[-1])


class LocalMemoryTests(unittest.TestCase):
    def test_cache_aggregation_missing_and_mbm_overlap(self):
        from ramtop.memory import cache_rates, mbm_rates, pressure, memory_fields
        events=[(Event('cpu0/cache references',0,{},0),100),
                (Event('cpu1/cache references',0,{},1),200),
                (Event('cpu0/cache misses',0,{},0),10),
                (Event('cpu1/cache misses',0,{},1),20)]
        self.assertEqual(cache_rates(events),{'cache references':300,'cache misses':30,'miss ratio':10})
        events[-1]=(events[-1][0],None)
        self.assertIsNone(cache_rates(events)['cache misses'])
        self.assertIsNone(cache_rates(events)['miss ratio'])
        mbm=[(Event('resctrl/mon0/mbm_total_bytes',0,{},0,unit='B/s',tier=3),100),
             (Event('resctrl/mon0/mbm_local_bytes',0,{},0,unit='B/s',tier=3),80)]
        self.assertEqual(mbm_rates(mbm),{'mbm_total_bytes':100,'mbm_local_bytes':80})
        self.assertEqual(pressure('some avg10=1.25 avg60=2 total=1\nfull avg10=0.50 total=1'),{'some':1.25,'full':.5})
        self.assertEqual(pressure('unavailable'),{})
        self.assertEqual(memory_fields('Dirty: 16 kB\nWriteback: 0 kB'),{'Dirty':16384,'Writeback':0})
        self.assertNotIn('Dirty',memory_fields('unavailable'))

    def test_amd_fallback_graphs_and_visible_process_table_at_80x24(self):
        from ramtop.app import Ramtop
        from textual.widgets import Sparkline, DataTable
        class FakeHardware:
            tier,notes,inventory=4,[],{}
            def sample(self,elapsed):
                return [(Event('cpu0/cache references',0,{},0),1000),
                        (Event('cpu0/cache misses',0,{},0),100)]
        async def exercise():
            app=Ramtop(FakeHardware())
            async with app.run_test(size=(80,24)) as pilot:
                await pilot.pause()
                self.assertFalse(app.query_one('#agents').display)
                self.assertTrue(app.query_one('#cache-performance').display)
                self.assertFalse(app.query_one('#mbm-performance').display)
                self.assertEqual(list(app.query_one('#cache-references-graph',Sparkline).data)[-1],1000)
                self.assertEqual(list(app.query_one('#cache-misses-graph',Sparkline).data)[-1],100)
                table=app.query_one('#processes',DataTable)
                self.assertGreaterEqual(table.size.height,4)
                self.assertLessEqual(table.region.bottom,23)
        asyncio.run(exercise())
