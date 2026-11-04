"""Persistent MAC-keyed local inventory. Neighbor sightings do not imply reachability."""
import datetime
import ipaddress
import json
from pathlib import Path
import re
import sqlite3
import threading


class DeviceHistory:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()
        with sqlite3.connect(self.path) as db:
            db.execute('CREATE TABLE IF NOT EXISTS devices (mac TEXT PRIMARY KEY, record TEXT NOT NULL)')

    def observe(self, devices, stamp, source, confirmed=False):
        # Store normalized UTC timestamps for consistent comparisons across data sources.
        stamp = datetime.datetime.fromisoformat(stamp.replace('Z', '+00:00')).astimezone(datetime.timezone.utc).isoformat()
        with self.lock, sqlite3.connect(self.path) as db:
            for device in devices:
                mac = str(device.get('mac') or '').lower()
                if not re.fullmatch(r'(?:[0-9a-f]{2}:){5}[0-9a-f]{2}', mac):
                    continue
                saved = db.execute('SELECT record FROM devices WHERE mac=?', (mac,)).fetchone()
                record = json.loads(saved[0]) if saved else {'mac': mac, 'first_observed': stamp, 'last_observed': stamp,
                    'last_confirmed': None, 'names': [], 'addresses': [], 'ports': [], 'sources': []}
                record['first_observed'] = min(record['first_observed'], stamp)
                record['last_observed'] = max(record['last_observed'], stamp)
                if confirmed or device.get('ping') or device.get('ports') or device.get('self'):
                    record['last_confirmed'] = max(record['last_confirmed'] or stamp, stamp)
                for name in {device.get(k) for k in ('name', 'hostname', 'router_name', 'nickName') if device.get(k)}:
                    previous = next((n for n in record['names'] if n['name'] == name and n['source'] == source), None)
                    if previous:
                        previous['first_seen'] = min(previous['first_seen'], stamp)
                        previous['last_seen'] = max(previous['last_seen'], stamp)
                    else:
                        record['names'].append({'name': name, 'source': source, 'first_seen': stamp, 'last_seen': stamp})
                for address in (device.get('ip'), device.get('ipv6')):
                    try:
                        address = str(ipaddress.ip_address(address))
                    except ValueError:
                        continue
                    if address not in record['addresses']:
                        record['addresses'].append(address)
                record['ports'] = sorted(set(record['ports'] + device.get('ports', [])))
                record['sources'] = sorted(set(record['sources'] + [source]))
                db.execute('INSERT OR REPLACE INTO devices VALUES (?, ?)', (mac, json.dumps(record, ensure_ascii=False)))

    def view(self):
        with self.lock, sqlite3.connect(self.path) as db:
            return [json.loads(row[0]) for row in db.execute('SELECT record FROM devices ORDER BY mac')]
