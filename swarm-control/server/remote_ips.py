"""Cached public-IP identity, without inferring visited websites from CDN addresses."""
import datetime
import json
from pathlib import Path
import queue
import re
import subprocess
import threading
import time
import urllib.request
from geography import public_ip


def reverse_dns(ip):
    command = ['dig', '-x', ip, '+time=2', '+tries=1', '+noall', '+answer', '+comments', '+stats']
    result = {'ptr':None, 'ptr_names':[], 'dns_engine':'dig', 'dns_command':' '.join(command), 'dns_ttl':60}
    try:
        reply = subprocess.run(command,capture_output=True,text=True,timeout=4)
        body = reply.stdout[:16384]
        status = re.search(r'status: ([A-Z]+)', body)
        code = status.group(1) if status else 'ERROR'
        answers = [line.split() for line in body.splitlines() if line and not line.startswith(';')]
        ptrs = [row for row in answers if len(row)>=5 and row[2:4]==['IN','PTR']]
        names = list(dict.fromkeys(row[4].rstrip('.') for row in ptrs))
        result.update(ptr=names[0] if names else None, ptr_names=names, dns_status=code,
            ptr_status='resolved' if names else 'No PTR record (NXDOMAIN)' if code=='NXDOMAIN' else
                'No PTR record (NOERROR)' if code=='NOERROR' else 'DNS lookup failed: '+code)
        if ptrs:
            result['dns_ttl'] = min(int(row[1]) for row in ptrs)
        elif code in ('NXDOMAIN','NOERROR'):
            result['dns_ttl'] = 3600
        resolver = re.search(r'^;; SERVER: (.+)$',body,re.M)
        if resolver: result['dns_resolver'] = resolver.group(1)
    except (OSError, subprocess.TimeoutExpired):
        result.update(dns_status='ERROR',ptr_status='dig unavailable or timed out')
    result['dns_cached_at'] = time.time()
    return result


def lookup(ip, previous=None):
    result = {**(previous or {}), 'ip':ip, **reverse_dns(ip)}
    result['sources'] = ['dig reverse-DNS PTR query']
    if result.get('asn') and time.time()-result.get('cached_at',0)<604800:
        result['sources'].append('ipwho.is network / IP-location database')
    else:
        try:
            request = urllib.request.Request('https://ipwho.is/'+ip,headers={'User-Agent':'LAN-monitor/1.0'})
            with urllib.request.urlopen(request, timeout=6) as response:
                data = json.loads(response.read(65536))
            if data.get('success') is not True:
                raise ValueError('Network lookup unavailable')
            connection = data.get('connection') or {}
            result.update(asn=connection.get('asn'),organization=connection.get('org'),isp=connection.get('isp'),
                          network_domain=connection.get('domain'),city=data.get('city'),region=data.get('region'),country=data.get('country'))
            result['sources'].append('ipwho.is network / IP-location database')
            result.pop('network_error',None)
        except Exception as exc:
            result['network_error'] = type(exc).__name__
        result['cached_at'] = time.time()
    result['observed'] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    return result


class RemoteIPs:
    def __init__(self, path):
        self.path = Path(path)
        self.lock = threading.Lock()
        self.pending = set()
        self.queue = queue.Queue()
        try:
            self.cache = json.loads(self.path.read_text())
        except (OSError, ValueError):
            self.cache = {}
        self.thread=threading.Thread(target=self._worker,daemon=True)
        self.thread.start()

    def start(self, address):
        ip = public_ip(address)
        if not ip:
            return False
        with self.lock:
            cached = self.cache.get(ip, {})
            ttl = min(86400, max(30, cached.get('dns_ttl',60)))
            if (cached.get('dns_engine') == 'dig' and time.time()-cached.get('dns_cached_at',0) < ttl) or ip in self.pending:
                return True
            if len(self.pending) >= 100:
                return False
            self.pending.add(ip)
            self.queue.put(ip)
        return True

    def diagnostics(self):
        with self.lock:return {'pending':len(self.pending),'worker_alive':self.thread.is_alive()}

    def view(self):
        with self.lock:
            return {**self.cache, **{ip:{'ip':ip,'status':'resolving'} for ip in self.pending if ip not in self.cache}}

    def _worker(self):
        while True:
            ip = self.queue.get()
            try:
                with self.lock:
                    previous = self.cache.get(ip)
                result = lookup(ip, previous)
                with self.lock:
                    self.cache[ip] = result
                    self.path.parent.mkdir(parents=True,exist_ok=True)
                    temporary = self.path.with_suffix('.tmp')
                    temporary.write_text(json.dumps(self.cache))
                    temporary.replace(self.path)
            except Exception:
                # One lookup or cache-write failure must not stop all subsequent DNS resolution.
                pass
            finally:
                with self.lock:
                    self.pending.discard(ip)
                self.queue.task_done()
            time.sleep(1)
