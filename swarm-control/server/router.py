"""Authenticated, bounded, read-only ASUS-compatible custom firmware telemetry."""
import base64
import datetime
import http.cookiejar
import json
import math
from pathlib import Path
import re
import time
import urllib.parse
import urllib.request

CONFIG = Path.home()/'.config/swarm-control/server/router.json'
LIMIT = 1024 * 1024


def number(value):
    try:
        result = float(value)
        return result if math.isfinite(result) and result >= 0 else None
    except (TypeError, ValueError):
        return None


def array(body, name):
    match = re.search(r'\b'+re.escape(name)+r'\s*=\s*(\[[^;]*\])\s*;', body)
    if not match:
        raise ValueError('Missing telemetry array')
    values = json.loads(match[1])
    if not isinstance(values, list) or len(values) > 32:
        raise ValueError('Invalid telemetry array')
    return [number(v) for v in values]


def health(body):
    memory, load, connections = array(body, 'mem_stats_arr'), array(body, 'cpu_stats_arr'), array(body, 'conn_stats_arr')
    if len(memory) < 10 or len(load) < 3 or len(connections) < 2:
        raise ValueError('Incomplete health telemetry')
    return {'memory_total_mb':memory[0], 'memory_free_mb':memory[1], 'memory_used_mb':memory[8],
            'memory_available_mb':memory[9], 'load_average_1_5_15':load[:3],
            'conntrack_entries':connections[0], 'active_connections':connections[1],
            'basis':'Firmware ajax_sysinfo.asp; memory units MB as labeled by Tools_Sysinfo.asp; load is not CPU percent'}


def temperatures(body):
    result = {}
    cpu = re.search(r'\bcurr_cpuTemp\s*=\s*"([0-9.]+)"', body)
    if cpu:
        result['cpu_celsius'] = number(cpu[1])
    for band in (0, 1):
        match = re.search(r'\bcurr_coreTmp_wl'+str(band)+r'_raw\s*=\s*"([0-9.]+)&deg;C"', body)
        if match:
            result['radio_'+str(band)+'_celsius'] = number(match[1])
    return result


def netdev_counters(values):
    result = {}
    for name in ('BRIDGE', 'WIRED', 'WIRELESS0', 'WIRELESS1'):
        pair = {}
        for direction in ('rx', 'tx'):
            value = values.get(name+'_'+direction)
            try:
                count = int(str(value), 16) if str(value).startswith('0x') else int(value)
                if 0 <= count < 2**64:
                    pair[direction+'_bytes'] = count
            except (ValueError, TypeError):
                pass
        if len(pair) == 2:
            result[name] = pair
    return result


def rates(current, previous, elapsed):
    result = {}
    for name, counters in current.items():
        row = dict(counters)
        for direction in ('rx', 'tx'):
            key = direction+'_bytes'
            old = previous.get(name, {}).get(key)
            row[direction+'_mbps'] = (counters[key]-old)*8/elapsed/1e6 if old is not None and counters[key] >= old and 0 < elapsed <= 60 else None
        result[name] = row
    return result


class Router:
    def __init__(self):
        self.config = json.loads(CONFIG.read_text())
        self.base = 'http://' + self.config['address']
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        self.opener.addheaders = [('User-Agent', 'Mozilla/5.0'), ('Referer', self.base+'/Main_Login.asp')]
        self.remembered_clients = []
        self.remembered_time = 0
        self.metadata = None
        self.metadata_time = 0
        self.previous_counters = {}
        self.previous_time = None

    def text(self, path):
        with self.opener.open(self.base+path, timeout=4) as response:
            body = response.read(LIMIT+1)
        if len(body) > LIMIT:
            raise ValueError('Telemetry response too large')
        return body.decode('utf-8-sig')

    def login(self):
        auth = base64.b64encode((self.config['username']+':'+self.config['password']).encode()).decode()
        request = urllib.request.Request(self.base+'/login.cgi', data=urllib.parse.urlencode({'login_authorization':auth,'next_page':'index.asp'}).encode())
        with self.opener.open(request, timeout=4) as response:
            response.read(LIMIT)

    def hook(self, name):
        return json.loads(self.text('/appGet.cgi?hook='+name))

    def read(self):
        for attempt in range(2):
            try:
                clients = self.hook('get_clientlist()')['get_clientlist']
                break
            except (ValueError, KeyError):
                if attempt:
                    raise
                self.login()
        known_clients = [{'mac':c.get('mac'), 'ip':c.get('ip'), 'name':c.get('name'), 'nickName':c.get('nickName')}
                         for c in list(clients.values())[:256] if isinstance(c, dict)]
        result = []
        for client in list(clients.values())[:256]:
            if not isinstance(client, dict) or str(client.get('isOnline')) != '1':
                continue
            mac = str(client.get('mac', '')).lower()
            if not re.fullmatch(r'(?:[0-9a-f]{2}:){5}[0-9a-f]{2}', mac):
                continue
            def rate(name):
                value = number(client.get(name))
                return value if value and value > 0 else None
            signal = client.get('rssi')
            try:
                signal = float(signal)
                signal = signal if -130 < signal < 0 else None
            except (ValueError, TypeError):
                signal = None
            result.append({'mac':mac, 'ip':client.get('ip'),
                           'name':client.get('nickName') or client.get('name'),
                           'wireless':str(client.get('isWL','0')) != '0',
                           'band':client.get('isWL'), 'tx_link_mbps':rate('curTx'),
                           'rx_link_mbps':rate('curRx'), 'signal_dbm':signal,
                           'connected_duration':client.get('wlConnectTime') or None,
                           'per_client_bytes':None})
        now = time.monotonic()
        errors = {}
        if not self.remembered_time or now-self.remembered_time >= 300:
            try:
                remembered = self.hook('get_clientlist_from_json_database()')['get_clientlist_from_json_database']
                self.remembered_clients = [{'mac':c.get('mac') or mac, 'name':c.get('name'), 'nickName':c.get('nickName')}
                    for mac,c in remembered.items() if isinstance(c,dict)]
                self.remembered_time = now
            except Exception as exc:
                errors['remembered_clients'] = type(exc).__name__
        known_clients.extend(self.remembered_clients)
        if self.metadata is None or now-self.metadata_time >= 300:
            try:
                names = ('productid', 'firmver', 'buildno', 'extendno', 'sw_mode', 'lan_hwaddr')
                self.metadata = {}
                for name in names:
                    self.metadata.update(self.hook('nvram_get('+name+')'))
                self.metadata_time = now
            except Exception as exc:
                errors['firmware'] = type(exc).__name__
        metadata = self.metadata or {}
        telemetry = {'firmware':{k:metadata.get(k) for k in ('productid','firmver','buildno','extendno','sw_mode')},
                     'sources':['appGet.cgi get_clientlist()', 'appGet.cgi whitelisted firmware fields'],
                     'per_client_traffic':('Firmware totalTx/totalRx fields are present but units/scope are unverified; not interpreted or merged' if any(isinstance(c,dict) and (c.get('totalTx') or c.get('totalRx')) for c in clients.values()) else 'Unavailable: client totalTx/totalRx fields are empty on this firmware in AP mode; gateway capture remains separate'),
                     'wan':'Not applicable in firmware AP mode' if metadata.get('sw_mode') == '3' else 'WAN health and negotiated capacity not measured',
                     'destination_records':'Not measured'}
        try:
            telemetry['health'] = health(self.text('/ajax_sysinfo.asp'))
            telemetry['sources'].append('ajax_sysinfo.asp')
        except Exception as exc:
            errors['health'] = type(exc).__name__
        try:
            telemetry['temperatures'] = temperatures(self.text('/ajax_coretmp.asp'))
            telemetry['sources'].append('ajax_coretmp.asp')
        except Exception as exc:
            errors['temperatures'] = type(exc).__name__
        try:
            counters = netdev_counters(self.hook('netdev()')['netdev'])
            sampled = time.monotonic()
            elapsed = sampled-self.previous_time if self.previous_time is not None else 0
            telemetry['interfaces'] = rates(counters, self.previous_counters, elapsed)
            telemetry['sample_seconds'] = elapsed if elapsed else None
            telemetry['interface_basis'] = 'Firmware netdev aggregate interface bytes and sampled decimal Mbps. RX/TX is from router perspective. Bridge/wired/radio groups overlap: never sum or add to gateway counters; not per-client or internet throughput.'
            self.previous_counters, self.previous_time = counters, sampled
            telemetry['sources'].append('appGet.cgi netdev()')
        except Exception as exc:
            errors['interfaces'] = type(exc).__name__
        try:
            body = self.text('/ajax_conntrack.asp')
            if re.search(r'\bconnarray\s*=\s*\[\[\]\]\s*;', body) and re.search(r'\bconnarray_route\s*=\s*\[\[\]\]\s*;', body):
                telemetry['destination_records'] = 'Unavailable: firmware connection arrays are empty in AP mode; no router destinations inferred'
            else:
                telemetry['destination_records'] = 'Firmware connection output present but unsupported format; not attributed or merged'
            telemetry['sources'].append('ajax_conntrack.asp')
        except Exception as exc:
            errors['connections'] = type(exc).__name__
        hardware_flows = None
        if self.config.get('hardware_flows'):
            try:
                from router_flows import read
                hardware_flows = read(self.config)
            except Exception as exc:
                errors['hardware_flows'] = type(exc).__name__
        stamp = datetime.datetime.now(datetime.timezone.utc).isoformat()
        telemetry.update(observed=stamp, errors=errors, stale_after_seconds=45)
        return {'mac':metadata.get('lan_hwaddr', '7c:10:c9:b3:c2:38').lower(), 'ip':self.config['address'],
                'hardware_flows':hardware_flows, 'clients':result, 'known_clients':known_clients, 'observed':stamp, 'telemetry':telemetry,
                'basis':'Authenticated firmware association table; reported PHY rates are not throughput'}
