#!/usr/bin/env python3
from paths import STATE
from cpu import CpuMonitor
from connections import Connections
from socket_owners import SocketOwners
from docker_downloads import DockerDownloads
from containers import ContainerMonitor
"""Ethernet-only LAN inventory, using standard Linux tools and Python."""
import concurrent.futures
import datetime
import ipaddress
import json
import mimetypes
from pathlib import Path
import socket
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit, parse_qs
from live_distributed import LiveDistributedTests
from master_api import MasterAPI
from master_http import handle as handle_master, authorize_dashboard
from benchmark import Benchmark, SIZE, PAYLOAD as BENCHMARK_PAYLOAD
from web_checks import WebChecks
from remote_map import collect
from measurements import link_info, route_trace
from router import Router
from history import DeviceHistory
from device_names import retain_device_names
from geography import Geography
from remote_ips import RemoteIPs
from capture_config import publish as publish_capture_config

INTERFACE = 'enp7s0'
PORT = 8089
lock = threading.Lock()
refresh = threading.Event()
map_refresh = threading.Event()
remote_ips = RemoteIPs(STATE / 'remote-ips.json')
geography = Geography(STATE / 'geography.json')
history = DeviceHistory(STATE / 'device-history.sqlite3')
state = {'devices': [], 'scanning': True, 'updated': None, 'error': None}


def snapshot():
    with lock:
        return dict(state)


distributed_tests = LiveDistributedTests()
def master_suites():
    from distributed_worker import SUITES
    return SUITES
master_api = MasterAPI(distributed_tests, snapshot, master_suites)
def resource_network():
    data=snapshot()
    data['interface']=INTERFACE
    try:data['link_traffic']=json.loads(STATE.joinpath('link-traffic.json').read_text())
    except (OSError,ValueError):data['link_traffic']=None
    return data


cpu_monitor = CpuMonitor(activity=distributed_tests.cpu_activity, network=resource_network)
connection_monitor = Connections()
socket_owners = SocketOwners()
docker_downloads = DockerDownloads()
container_monitor = ContainerMonitor()
benchmark = Benchmark()
checks = WebChecks(snapshot)
PAYLOAD = bytes(4194304)


def map_worker():
    while True:
        map_refresh.clear()
        data = snapshot()
        if data.get('updated'):
            with lock:
                state['mapping'] = True
            try:
                local_route = route_trace()
                result = collect(data['devices'], INTERFACE)
                for record in result.values():
                    if record.get('status') == 'measured':
                        history.observe(record.get('devices', []), record['observed'], 'SSH neighbor discovery')
                        history.observe([{'mac':record.get('via_mac'), 'ip':record.get('local_ip'), 'hostname':record.get('hostname')}],
                                        record['observed'], 'SSH device identity', confirmed=True)
                with lock:
                    state['perspectives'] = result
                    state['route'] = local_route
            finally:
                with lock:
                    state['mapping'] = False
            map_refresh.wait(300)
        else:
            map_refresh.wait(2)


def router_worker():
    router = None
    while True:
        try:
            router = router or Router()
            result = router.read()
            history.observe(result.get('known_clients', []), result['observed'], 'ASUS remembered client')
            history.observe(result.get('clients', []), result['observed'], 'ASUS active client', confirmed=True)
            with lock:
                state['router'] = result
                state['router_error'] = None
        except Exception as exc:
            with lock:
                state['router_error'] = str(exc)
        time.sleep(15)


def traffic_worker():
    previous = {}
    while True:
        now = time.monotonic()
        interfaces = {INTERFACE, snapshot().get('upstream', {}).get('interface')}
        rates = {}
        for interface in interfaces - {None}:
            try:
                counts = [int(Path(f'/sys/class/net/{interface}/statistics/{direction}_bytes').read_text()) for direction in ('rx', 'tx')]
                if interface in previous:
                    elapsed = now - previous[interface][0]
                    values = [max(0, (value-old)*8/elapsed/1e6) for value, old in zip(counts, previous[interface][1])]
                    rates[interface] = {'rx_mbps': values[0], 'tx_mbps': values[1], 'interface': interface,
                                        'sample_seconds': elapsed, 'updated': datetime.datetime.now(datetime.timezone.utc).isoformat()}
                previous[interface] = now, counts
            except OSError:
                pass
        with lock:
            state['interface_traffic'] = rates
            state['traffic'] = rates.get(INTERFACE)
        time.sleep(1)


def run(*args, timeout=4):
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout)


def network():
    links = json.loads(run('ip', '-j', '-4', 'addr', 'show', 'dev', INTERFACE).stdout)
    address = next(a for a in links[0]['addr_info'] if a['scope'] == 'global')
    local = address['local']
    subnet = ipaddress.ip_network(f"{local}/{address['prefixlen']}", strict=False)
    if subnet.prefixlen < 24:
        raise ValueError('Discovery is limited to an Ethernet subnet of /24 or smaller.')
    return local, subnet


def probe(ip):
    return run('ping', '-n', '-I', INTERFACE, '-c', '1', '-W', '1', ip).returncode == 0


def details(peer, local, alive):
    ip = peer['dst']
    ipv6 = ':' in ip
    ports = []
    for port in (22, 80, 443):
        try:
            with socket.socket(socket.AF_INET6 if ipv6 else socket.AF_INET) as conn:
                conn.settimeout(.6)
                if ipv6:
                    scope = socket.if_nametoindex(INTERFACE)
                    conn.bind((local, 0, 0, scope))
                    conn.connect((ip, port, 0, scope))
                else:
                    conn.bind((local, 0))
                    conn.connect((ip, port))
                ports.append(port)
        except OSError:
            pass
    # Ask Avahi only on the Ethernet interface, never via Wi-Fi or Docker.
    hostname = None
    try:
        result = run('busctl', '--timeout=1', 'call', '--', 'org.freedesktop.Avahi', '/',
                     'org.freedesktop.Avahi.Server', 'ResolveAddress', 'iisu',
                     str(socket.if_nametoindex(INTERFACE)), '1' if ipv6 else '0', ip, '0', timeout=2)
        if result.returncode == 0:
            import shlex
            hostname = shlex.split(result.stdout)[5]
    except subprocess.TimeoutExpired:
        pass
    return {'ip': ip, 'mac': peer.get('lladdr'), 'hostname': hostname,
            'neighbor': peer.get('state', []), 'ping': ip in alive,
            'ports': ports, 'self': False}


def scan():
    local, subnet = network()
    multicast = run('ping', '-6', '-n', '-c', '2', '-W', '1', 'ff02::1%' + INTERFACE)
    with concurrent.futures.ThreadPoolExecutor(max_workers=32) as pool:
        addresses = [str(ip) for ip in subnet.hosts() if str(ip) != local]
        alive = {ip for ip, ok in zip(addresses, pool.map(probe, addresses)) if ok}
    peers = json.loads(run('ip', '-j', '-4', 'neigh', 'show', 'dev', INTERFACE).stdout)
    peers = [p for p in peers if p.get('lladdr') and ipaddress.ip_address(p['dst']) in subnet]
    with concurrent.futures.ThreadPoolExecutor(max_workers=16) as pool:
        devices = list(pool.map(lambda p: details(p, local, alive), peers))
    devices.sort(key=lambda d: ipaddress.ip_address(d['ip']))
    v6links = json.loads(run('ip', '-j', '-6', 'addr', 'show', 'dev', INTERFACE).stdout)
    v6local = next((a['local'] for a in v6links[0]['addr_info'] if a['scope'] == 'link'), None)
    if v6local:
        v6peers = json.loads(run('ip', '-j', '-6', 'neigh', 'show', 'dev', INTERFACE).stdout)
        for peer in v6peers:
            if peer.get('lladdr'):
                item = details(peer, v6local, set())
                item['ping'] = ('from ' + peer['dst'] + '%') in multicast.stdout
                existing = next((d for d in devices if d['mac'] == item['mac']), None)
                if existing:
                    existing['ipv6'] = peer['dst']
                    existing['ports'] = sorted(set(existing['ports'] + item['ports']))
                    existing['hostname'] = existing['hostname'] or item['hostname']
                    existing['ping'] = existing['ping'] or item['ping']
                    if 22 in item['ports']:
                        existing['ssh_ip'] = peer['dst']
                else:
                    devices.append(item)
    devices.insert(0, {'ip': local, 'mac': Path(f'/sys/class/net/{INTERFACE}/address').read_text().strip(),
                      'hostname': socket.gethostname() + '.local', 'neighbor': [],
                      'ping': True, 'ports': [22] if port_open(local, 22) else [], 'self': True})
    routes = json.loads(run('ip', '-j', '-4', 'route', 'show', 'default').stdout)
    upstream = next((r for r in routes if r.get('gateway')), {})
    return {'devices': devices, 'interface': INTERFACE, 'subnet': str(subnet), 'gateway': local,
            'upstream': {'ip': upstream.get('gateway'), 'interface': upstream.get('dev')},
            'links': {name:link_info(name) for name in {INTERFACE,upstream.get('dev')} if name},
            'updated': datetime.datetime.now(datetime.timezone.utc).isoformat(), 'error': None}


def port_open(host, port):
    try:
        with socket.create_connection((host, port), timeout=1):
            return True
    except OSError:
        return False


def worker():
    while True:
        refresh.clear()
        with lock:
            state['scanning'] = True
        try:
            result = scan()
            # Record only fresh observations, so a cache hit cannot renew its own TTL.
            history.observe(result['devices'], result['updated'], 'Ethernet discovery')
            result['devices']=retain_device_names(result['devices'],history.view(),result['updated'])
            publish_capture_config(INTERFACE)
            with lock:
                state.update(result)
        except Exception as exc:
            with lock:
                state['error'] = str(exc)
        finally:
            with lock:
                state['scanning'] = False
        refresh.wait(30)


class Handler(BaseHTTPRequestHandler):
    def do_DELETE(self):
        if not handle_master(self,master_api):self.send_error(405)

    def do_OPTIONS(self):
        if not handle_master(self,master_api):self.send_error(405)

    def do_GET(self):
        if handle_master(self,master_api):return
        path = urlsplit(self.path).path
        if path.count('/')==1 and Path(path).suffix in ('.html','.js','.css','.svg','.png','.geojson') and (Path(__file__).with_name('view')/path[1:]).is_file():
            asset=Path(__file__).with_name('view')/path[1:]
            if asset.is_symlink():self.send_error(404);return
            body=asset.read_bytes();content_type=mimetypes.guess_type(asset.name)[0] or 'application/octet-stream'
        elif path in ('/api/benchmark/ping', '/api/benchmark/download'):
            kind = path.rsplit('/', 1)[1]
            token = parse_qs(urlsplit(self.path).query).get('token', [''])[0]
            if not benchmark.claim(token, kind):
                self.send_error(429, 'Benchmark expired or request limit reached')
                return
            body = BENCHMARK_PAYLOAD if kind == 'download' else b'ok'
            content_type = 'application/octet-stream'
        elif path == '/api/containers':
            query = parse_qs(urlsplit(self.path).query)
            try:
                body = json.dumps(container_monitor.view(query.get('host', [''])[0], query.get('id', [''])[0])).encode()
            except (OSError, ValueError) as exc:
                self.send_error(400, str(exc))
                return
            content_type = 'application/json'
        elif path == '/api/connections':
            body = json.dumps(connection_monitor.view()).encode()
            content_type = 'application/json'
        elif path == '/api/wgs':
            from wgs_progress import snapshot as wgs_snapshot
            body = json.dumps(wgs_snapshot()).encode()
            content_type = 'application/json'
        elif path == '/api/cpu':
            body = json.dumps(cpu_monitor.view()).encode()
            content_type = 'application/json'
        elif path == '/api/distributed-tests/artifacts':
            query = parse_qs(urlsplit(self.path).query)
            try:
                body, content_type = distributed_tests.artifact(*(query.get(key, [''])[0] for key in ('run','host','suite','name')))
            except (ValueError, FileNotFoundError):
                self.send_error(404, 'Visual artifact not found')
                return
        elif path == '/api/mobile-tests':
            from mobile_worker import view
            data=view()
            data['requests']=[{'id':r['id'],'status':r['status']} for r in distributed_tests.requests.records() if 'mobile' in r.get('selection',{}).get('suites',[]) and r['status'] in ('queued','running')]
            body=json.dumps(data).encode()
            content_type='application/json'
        elif path == '/api/distributed-tests':
            try:
                data = distributed_tests.view(parse_qs(urlsplit(self.path).query).get('id', [None])[0])
            except ValueError as exc:
                self.send_error(400, str(exc))
                return
            body = json.dumps(data).encode()
            content_type = 'application/json'
        elif path == '/api/devices':
            data = snapshot()
            data['checks'] = checks.view()
            data['device_history'] = history.view()
            data['geography'] = geography.view()
            data['remote_ips'] = remote_ips.view()
            try:
                data['link_traffic'] = json.loads(STATE.joinpath('link-traffic.json').read_text())
            except (OSError, ValueError):
                data['link_traffic'] = None
            data['process_resources'] = socket_owners.view_context()
            data['docker_downloads'] = docker_downloads.view()
            own_mac = next((d.get('mac') for d in data.get('devices', []) if d.get('self')), None)
            data['host_attribution'] = socket_owners.annotate((data.get('link_traffic') or {}).get('flows', {}).get('records', []), own_mac)
            socket_owners.annotate((data.get('link_traffic') or {}).get('uplink_flows', {}).get('records', []), own_mac)
            # Resolve observed remote destinations in the background, including unselected nodes.
            relationships = (data.get('link_traffic') or {}).get('outbound', {}).get('relationships', [])
            if parse_qs(urlsplit(self.path).query).get('resolve', ['1'])[0] != '0':
                # Queue each active address once; repeated sources cannot starve quieter IPv6 destinations.
                flows = (data.get('link_traffic') or {}).get('flows', {}).get('records', [])
                addresses = list(dict.fromkeys([row.get('remote_ip') for row in sorted(flows, key=lambda row:row.get('download_mbps',0)+row.get('upload_mbps',0), reverse=True)] + [row.get('destination') for row in sorted(relationships, key=lambda row:row.get('bytes',0), reverse=True)]))
                for address in addresses:
                    remote_ips.start(address)
                for hop in data.get('route', {}).get('hops', []):
                    remote_ips.start(hop.get('ip'))
            data['name_resolution'] = remote_ips.diagnostics()
            # The loopback-only collector trusts the single local nginx proxy.
            source = self.headers.get('X-Real-IP', self.client_address[0]).split('%')[0]
            own = next((d for d in data['devices'] if d['self']), None)
            viewer = next((d for d in data['devices'] if source in (d['ip'], d.get('ipv6'), d.get('ssh_ip'))), None)
            if source in ('127.0.0.1', '::1'):
                viewer = own
            data['viewer'] = {'id': (viewer.get('mac') or viewer['ip']) if viewer else 'ip:'+source,
                              'ip': source, 'recognized': viewer is not None}
            if own:
                views = dict(data.get('perspectives', {}))
                views[own['mac']] = {'status': 'measured', 'via_mac': own['mac'],
                                     'hostname': own['hostname'], 'interface': INTERFACE,
                                     'local_ip': own['ip'], 'subnet': data.get('subnet'),
                                     'gateway': data.get('upstream', {}).get('ip'),
                                     'gateway_interface': data.get('upstream', {}).get('interface'),
                                     'devices': [d for d in data['devices'] if not d['self']],
                                     'traffic': data.get('traffic'), 'route': data.get('route'), 'link':data.get('links',{}).get(INTERFACE), 'observed': data.get('updated')}
                data['perspectives'] = views
            body = json.dumps(data).encode()
            content_type = 'application/json'
        elif path == '/api/test-payload':
            body = PAYLOAD
            content_type = 'application/octet-stream'
        elif path == '/api/health':
            body = json.dumps({'status': 'ok', 'scanning': snapshot()['scanning']}).encode()
            content_type = 'application/json'
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header('Content-Type', content_type)
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_POST(self):
        if handle_master(self,master_api):return
        path = urlsplit(self.path).path
        if path in ('/api/mobile-tests/start','/api/distributed-tests/start','/api/distributed-tests/stop','/api/distributed-tests/rerun','/api/telemetry/transport') and not authorize_dashboard(self,local_settings=path=='/api/telemetry/transport'):return
        if path=='/api/telemetry/transport':
            if self.headers.get('Origin')!='http://'+self.headers.get('Host',''):
                self.send_error(403);return
            try:
                length=int(self.headers.get('Content-Length','0'))
                if not 0<length<=256:raise ValueError('Invalid policy body')
                options=json.loads(self.rfile.read(length))
                if not isinstance(options,dict) or set(options)!={'transport'}:raise ValueError('Choose TCP or WebRTC')
                body=json.dumps(cpu_monitor.set_telemetry_transport(options['transport'])).encode()
            except ValueError as error:self.send_error(400,str(error));return
            except (OSError,RuntimeError):self.send_error(503,'Launcher transport settings are unavailable');return
            self.send_response(200);self.send_header('Content-Type','application/json')
            self.send_header('Cache-Control','no-store');self.send_header('Content-Length',str(len(body)))
            self.end_headers();self.wfile.write(body);return
        if path=='/api/mobile-tests/start':
            if self.headers.get('Origin')!='http://'+self.headers.get('Host',''):
                self.send_error(403);return
            try:
                result=distributed_tests.requests.submit(suites=['mobile'],force=True)
                body=json.dumps(result).encode()
                self.send_response(202);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
            except Exception as error:self.send_error(400,str(error))
            return
        if path=='/api/distributed-tests/rerun':
            if self.headers.get('Origin')!='http://'+self.headers.get('Host',''):
                self.send_error(403);return
            try:
                length=int(self.headers.get('Content-Length','0'))
                if not 0<length<=4096:raise ValueError('Invalid body length')
                args=json.loads(self.rfile.read(length))
                operation='rerun_suite' if 'suite' in args else 'rerun_failed'
                result=master_api.call(operation,args)
                body=json.dumps(result).encode()
                self.send_response(202);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
            except Exception:
                self.send_error(400,'Rerun could not be queued; check the selected run, host and suite')
            return
        if path not in ('/api/resolve', '/api/geography', '/api/refresh', '/api/web-checks', '/api/benchmark/start', '/api/benchmark/upload', '/api/distributed-tests/start', '/api/distributed-tests/stop'):
            self.send_error(404)
            return
        origin = self.headers.get('Origin')
        if origin and origin != 'http://' + self.headers.get('Host', ''):
            self.send_error(403)
            return
        if path in ('/api/distributed-tests/start', '/api/distributed-tests/stop'):
            if origin != 'http://' + self.headers.get('Host', ''):
                self.send_error(403, 'A same-origin dashboard request is required')
                return
            try:
                length=int(self.headers.get('Content-Length','0'))
                if not 0<=length<=4096:raise ValueError('Invalid body length')
                options=json.loads(self.rfile.read(length)) if length else {}
                if not isinstance(options,dict) or set(options)-{'force'} or type(options.get('force',False)) is not bool:raise ValueError('Invalid start options')
                started = distributed_tests.stop() if path.endswith('/stop') else distributed_tests.start(force=options.get('force',False))
            except (OSError, ValueError) as exc:
                self.send_error(400, str(exc))
                return
            body = json.dumps(distributed_tests.view()).encode()
            self.send_response(202 if started else 409)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path == '/api/resolve':
            ip = parse_qs(urlsplit(self.path).query).get('ip', [''])[0]
            if not remote_ips.start(ip):
                self.send_error(400, 'A public IP address is required or lookup queue is full')
                return
            self.send_response(202)
            self.end_headers()
            return
        if self.path == '/api/benchmark/start':
            token = benchmark.start()
            if token is None:
                self.send_error(429, 'Another benchmark is active; retry after 45 seconds')
                return
            body = json.dumps({'token': token, 'bytes': SIZE}).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path == '/api/benchmark/upload':
            if self.headers.get('Content-Length') != str(SIZE):
                self.send_error(400, 'Expected exactly 4 MiB')
                return
            if not benchmark.claim(self.headers.get('X-Benchmark-Token'), 'upload'):
                self.send_error(429, 'Benchmark expired or request limit reached')
                return
            self.connection.settimeout(5)
            try:
                remaining = SIZE
                deadline = time.monotonic() + 8
                while remaining and time.monotonic() < deadline:
                    chunk = self.rfile.read(min(65536, remaining))
                    if not chunk:
                        break
                    remaining -= len(chunk)
                if remaining:
                    self.send_error(408, 'Upload incomplete or timed out')
                    return
            except (TimeoutError, OSError):
                self.send_error(408, 'Upload timed out')
                return
            self.send_response(200)
            self.send_header('Content-Length', '2')
            self.end_headers()
            self.wfile.write(b'ok')
            return
        if self.path == '/api/geography':
            data = snapshot()
            try:
                data['link_traffic'] = json.loads(STATE.joinpath('link-traffic.json').read_text())
            except (OSError, ValueError):
                pass
            geography.start(data)
        elif self.path == '/api/web-checks':
            checks.start()
        else:
            refresh.set()
            map_refresh.set()
        self.send_response(202)
        self.end_headers()


if __name__ == '__main__':
    threading.Thread(target=connection_monitor.worker, daemon=True).start()
    threading.Thread(target=cpu_monitor.worker, daemon=True).start()
    threading.Thread(target=socket_owners.worker, daemon=True).start()
    threading.Thread(target=docker_downloads.worker, daemon=True).start()
    threading.Thread(target=worker, daemon=True).start()
    threading.Thread(target=traffic_worker, daemon=True).start()
    threading.Thread(target=map_worker, daemon=True).start()
    threading.Thread(target=router_worker, daemon=True).start()
    distributed_tests.staging.start()
    distributed_tests.requests.start()
    print(f'LAN collector: http://127.0.0.1:{PORT}', flush=True)
    ThreadingHTTPServer(('127.0.0.1', PORT), Handler).serve_forever()
