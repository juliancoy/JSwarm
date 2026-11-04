"""Opt-in approximate public-IP geography; no private addresses leave the host."""
import datetime
import ipaddress
import json
import math
from pathlib import Path
import threading
import time
import urllib.request


def public_ip(value):
    try:
        address = ipaddress.ip_address(value)
        return str(address) if address.is_global and not address.is_multicast else None
    except ValueError:
        return None


def location(value):
    lat, lon = value.get('latitude'), value.get('longitude')
    if value.get('success') is not True or not all(isinstance(v, (float, int)) and not isinstance(v, bool) and math.isfinite(v) for v in (lat, lon)):
        raise ValueError('Location unavailable')
    if not -90 <= lat <= 90 or not -180 <= lon <= 180:
        raise ValueError('Coordinates out of range')
    return {key:value.get(key) for key in ('ip','latitude','longitude','city','region','country')}


class Geography:
    def __init__(self, path):
        self.path = Path(path)
        self.lock = threading.Lock()
        self.running = False
        try:
            self.cache = json.loads(self.path.read_text())
        except (OSError, ValueError):
            self.cache = {}

    def view(self):
        with self.lock:
            return {'locations':dict(self.cache), 'running':self.running,
                    'basis':'Approximate IP geolocation via ipwho.is; anycast, VPNs and ISP routing can misplace endpoints. LAN devices share the estimated gateway site, not individual GPS positions.'}

    def start(self, data):
        ips = []
        for record in data.get('perspectives', {}).values():
            ips += [hop.get('ip') for hop in record.get('route', {}).get('hops', [])]
        ips += [hop.get('ip') for hop in data.get('route', {}).get('hops', [])]
        ips += [row.get('destination') for row in (data.get('link_traffic') or {}).get('outbound', {}).get('relationships', [])]
        ips = list(dict.fromkeys(ip for ip in map(public_ip, ips) if ip))[:100]
        with self.lock:
            if self.running:
                return
            self.running = True
        threading.Thread(target=self._worker, args=(['site']+ips,), daemon=True).start()

    def _worker(self, ips):
        try:
            for ip in ips:
                with self.lock:
                    previous = self.cache.get(ip, {})
                age = time.time()-previous.get('cached_at', 0)
                if age < (604800 if previous.get('latitude') is not None else 3600):
                    continue
                try:
                    request = urllib.request.Request('https://ipwho.is/'+('' if ip == 'site' else ip), headers={'User-Agent':'LAN-monitor/1.0'})
                    with urllib.request.urlopen(request, timeout=6) as response:
                        result = location(json.loads(response.read(65536)))
                    result['source'] = 'ipwho.is · approximate IP location'
                except Exception as exc:
                    result = {'error':type(exc).__name__}
                result.update(cached_at=time.time(), observed=datetime.datetime.now(datetime.timezone.utc).isoformat())
                with self.lock:
                    self.cache[ip] = result
                    self.path.parent.mkdir(parents=True, exist_ok=True)
                    temporary = self.path.with_suffix('.tmp')
                    temporary.write_text(json.dumps(self.cache))
                    temporary.replace(self.path)
                time.sleep(1)
        finally:
            with self.lock:
                self.running = False
