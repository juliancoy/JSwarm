"""Recent host socket ownership and bounded file/database resource context."""
import ipaddress
from pathlib import Path
import re
import subprocess
import threading
import time
from datetime import datetime
from process_resources import resource_context,process_start,program_identity


def endpoint(value):
    address, port = value.rsplit(':', 1)
    ip = ipaddress.ip_address(address.strip('[]').split('%')[0])
    return str(getattr(ip, 'ipv4_mapped', None) or ip), int(port)


def parse_owners(output):
    rows = {}
    for line in output.splitlines():
        parts = line.split()
        if len(parts) < 7 or parts[0] not in ('tcp', 'udp'):
            continue
        try:
            local, lp = endpoint(parts[4]); remote, rp = endpoint(parts[5])
        except ValueError:
            continue
        owners = [{'name': name, 'pid': int(pid)} for name, pid in re.findall(r'\("([^"\n]+)",pid=(\d+)', line)]
        if owners:
            rows[(6 if parts[0] == 'tcp' else 17, local, lp, remote, rp)] = owners[:4]
    return rows


POLL_SECONDS = 0.5
RETENTION_SECONDS = 300
MATCH_TOLERANCE = 1.5
MAX_TUPLES = 4096


def signature(owners):
    return tuple(sorted((p['pid'],p.get('start_ticks'),p['name'],p.get('script','')) for p in owners))


class SocketOwners:
    def __init__(self):
        self.lock = threading.Lock()
        self.rows = {}
        self.history = {}
        self.sampled_at = 0
        self.scan_status = 'unavailable'
        self.resources = {'processes':{}}
        self.resources_at = 0

    def remember(self, rows, sampled):
        with self.lock:
            self.rows = rows
            self.sampled_at = sampled
            self.scan_status = 'available'
            for key, owners in rows.items():
                windows = self.history.setdefault(key,[])
                sig = signature(owners)
                if windows and windows[-1]['signature']==sig and sampled-windows[-1]['last']<=MATCH_TOLERANCE:
                    windows[-1]['last'] = sampled
                else:
                    windows.append({'first':sampled,'last':sampled,'owners':owners,'signature':sig})
                self.history[key] = windows[-4:]
            for key in list(self.history):
                windows = [w for w in self.history[key] if sampled-w['last']<RETENTION_SECONDS]
                if windows:self.history[key] = windows
                else:del self.history[key]
            if len(self.history)>MAX_TUPLES:
                oldest=sorted(self.history,key=lambda k:self.history[k][-1]['last'])
                for key in oldest[:len(self.history)-MAX_TUPLES]:del self.history[key]

    def annotate(self, flows, host_mac):
        now=time.time()
        with self.lock:
            history={k:[dict(w) for w in windows] for k,windows in self.history.items()}
            sampled=self.sampled_at;status=self.scan_status
        counts={'current':0,'retained':0,'ambiguous':0,'unknown':0}
        download=attributed=0
        for flow in flows:
            if flow.get('device_mac')!=host_mac:continue
            for field in ('recent_processes','process_sampled_at','process_attribution'):
                flow.pop(field,None)
            rate=flow.get('download_mbps',0)
            if isinstance(rate,(int,float)) and rate>0:download+=rate
            key=tuple(flow.get(k) for k in ('protocol','local_ip','local_port','remote_ip','remote_port'))
            try:
                packet_at=datetime.fromisoformat(flow['last_seen']).timestamp()
            except (ValueError,KeyError,TypeError):
                packet_at=None
            candidates=[w for w in history.get(key,[]) if packet_at is not None
                        and now-w['last']<RETENTION_SECONDS
                        and w['first']-MATCH_TOLERANCE<=packet_at<=w['last']+MATCH_TOLERANCE]
            identities={w['signature'] for w in candidates}
            if len(identities)>1:
                state='ambiguous'
            elif len(identities)==1:
                match=max(candidates,key=lambda w:w['last'])
                state='current' if status=='available' and now-match['last']<MATCH_TOLERANCE else 'retained'
                flow['recent_processes']=match['owners']
                flow['process_sampled_at']=match['last']
                if isinstance(rate,(int,float)) and rate>0:attributed+=rate
            else:
                state='unknown'
            flow['process_attribution']=state
            flow['process_attribution_scope']='Owner observed near last packet, not ownership of all retained bytes'
            counts[state]+=1
        return {'status':status if now-sampled<3 else 'unavailable','sampled_at':sampled,
                'host_only':True,'counts':counts,'poll_seconds':POLL_SECONDS,
                'retention_seconds':RETENTION_SECONDS,'match_tolerance_seconds':MATCH_TOLERANCE,
                'host_download_mbps':download,'host_attributed_download_mbps':attributed,
                'host_download_coverage_percent':100*attributed/download if download>0 else None}

    def view_context(self):
        with self.lock:
            if time.time()-self.resources_at>10:
                return {'status':'stale','processes':{}}
            return {**self.resources,'sampled_at':self.resources_at,'status':self.resources.get('status','live')}

    def resource_worker(self):
        while True:
            with self.lock:
                rows=dict(self.rows)
            try:
                resources=resource_context(rows)
            except (OSError,ValueError):
                resources={'processes':{},'status':'unavailable'}
            with self.lock:
                self.resources=resources;self.resources_at=time.time()
            time.sleep(3)

    def worker(self):
        threading.Thread(target=self.resource_worker,daemon=True).start()
        while True:
            start=time.monotonic()
            try:
                result=subprocess.run(['ss','-Hntup'],capture_output=True,text=True,timeout=2,check=True)
                rows=parse_owners(result.stdout)
                identities={}
                for owners in rows.values():
                    for owner in owners:
                        pid=owner['pid']
                        if pid not in identities:
                            root=Path('/proc')/str(pid)
                            identities[pid]={'start_ticks':process_start(root),**program_identity(root)}
                        identity=identities[pid]
                        owner['start_ticks']=identity['start_ticks']
                        if identity.get('script'):owner['script']=identity['script']
                rows={key:[p for p in owners if p.get('start_ticks') is not None] for key,owners in rows.items()}
                rows={key:owners for key,owners in rows.items() if owners}
                self.remember(rows,time.time())
            except (OSError,ValueError,subprocess.SubprocessError):
                with self.lock:
                    self.rows={};self.scan_status='unavailable'
            time.sleep(max(.05,POLL_SECONDS-(time.monotonic()-start)))
