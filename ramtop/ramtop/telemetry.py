"""Headless, dependency-free swarm telemetry using the same ramtop PMU backend.

No Textual, psutil, active LAN probing, packet capture or address profiling is
required by the node collector. Records describe only counters visible to this
agent; a container PID namespace can limit process/connection ownership coverage.
"""
from pathlib import Path
import ipaddress
import json
import math
import os
import socket
import subprocess
import time
from .hardware import Hardware, read
from .memory import cache_rates, mbm_rates, pressure, memory_fields
from .gpu import GpuMonitor
from .connections import parse_ss

SCHEMA_VERSION = 1
MAX_PROCESSES = 48
MAX_CONNECTIONS = 96
MAX_NEIGHBORS = 96


def delta(value, before, elapsed):
    return (value-before)/elapsed if value is not None and before is not None and value>=before and elapsed>0 else None


def brief(value):
    """Project the aggregate fields safe to repeat in five-minute history."""
    result={key:value[key] for key in ('schema_version','sampled_at','status','scope','memory') if key in value}
    if 'memory' in result:result['memory']={k:v for k,v in result['memory'].items() if k!='counters'}
    return result


def valid_snapshot(value):
    if not isinstance(value,dict) or value.get('schema_version')!=SCHEMA_VERSION:return False
    if set(value)-{'schema_version','sampled_at','status','scope','memory','processes','connections','lan','filesystems','gpu','notes','limits'}:return False
    if value.get('status') not in ('live','warming','unavailable'):return False
    if type(value.get('sampled_at')) not in (int,float) or not math.isfinite(value['sampled_at']):return False
    if not isinstance(value.get('memory'),dict) or not isinstance(value.get('scope'),str) or len(value['scope'])>1000:return False
    for key,limit in [('processes',MAX_PROCESSES),('connections',MAX_CONNECTIONS),('lan',MAX_NEIGHBORS),('filesystems',24)]:
        rows=value.get(key,[])
        if not isinstance(rows,list) or len(rows)>limit or any(not isinstance(row,dict) for row in rows):return False
    return len(json.dumps(value,allow_nan=False))<=100000


class PerformanceSampler:
    def __init__(self):
        self.hardware=Hardware()
        self.gpu=None
        self.gpu_attempted=False
        self.previous_processes={}
        self.previous_connections={}
        self.last=time.monotonic()
        self.previous_faults={}
        self.previous_swap={}

    def processes(self, elapsed):
        rows=[];current={};denied=0
        for path in Path('/proc').glob('[0-9]*'):
            try:
                raw=(path/'stat').read_text();start,end=raw.index('('),raw.rindex(')')
                fields=raw[end+2:].split();key=(int(path.name),int(fields[19]))
                values={k:int(v.strip()) for k,v in (line.split(':',1) for line in (path/'io').read_text().splitlines())}
                old=self.previous_processes.get(key,{})
                row={'pid':key[0],'name':raw[start+1:end][:80],'rss_bytes':max(0,int(fields[21]))*os.sysconf('SC_PAGE_SIZE'),
                     'read_bytes_per_second':delta(values.get('read_bytes'),old.get('read_bytes'),elapsed),
                     'write_bytes_per_second':delta(values.get('write_bytes'),old.get('write_bytes'),elapsed),
                     'read_chars_per_second':delta(values.get('rchar'),old.get('rchar'),elapsed),
                     'write_chars_per_second':delta(values.get('wchar'),old.get('wchar'),elapsed),
                     'cancelled_write_bytes':values.get('cancelled_write_bytes')}
                rows.append(row);current[key]=values
            except PermissionError:denied+=1
            except (OSError,ValueError,IndexError):continue
        self.previous_processes=current
        # Preserve top consumers of RSS, reads and writes rather than only one resource.
        selected={}
        for field in ('rss_bytes','read_bytes_per_second','write_bytes_per_second'):
            for row in sorted(rows,key=lambda r:r.get(field) or 0,reverse=True)[:16]:selected[row['pid']]=row
        return list(selected.values())[:MAX_PROCESSES],len(rows),denied

    def sockets(self, elapsed):
        try:
            result=subprocess.run(['ss','-tunipeHO'],capture_output=True,text=True,timeout=3)
            if result.returncode:raise OSError('ss did not return socket counters')
            current=parse_ss(result.stdout)
        except (OSError,subprocess.SubprocessError) as error:
            self.previous_connections={}
            return [],str(error)[:160]
        rows=[];previous={}
        for flow in current:
            old=self.previous_connections.get(flow['key'],{})
            identifiable=flow['key'][1] not in (None,0) or flow['key'][2] not in (None,'0')
            row={k:flow[k] for k in ('protocol','state','local','remote','recvq','sendq','rtt')}
            row['owners']=[{'pid':pid,'name':name[:80]} for pid,name in flow['owners'][:8]]
            row['rx_bytes_per_second']=delta(flow['rx'],old.get('rx'),elapsed) if identifiable else None
            row['tx_acked_bytes_per_second']=delta(flow['tx'],old.get('tx'),elapsed) if identifiable else None
            rows.append(row);previous[flow['key']]=flow
        self.previous_connections=previous
        rows.sort(key=lambda r:(r['rx_bytes_per_second'] or 0)+(r['tx_acked_bytes_per_second'] or 0),reverse=True)
        return rows[:MAX_CONNECTIONS],''

    @staticmethod
    def neighbors():
        try:
            result=subprocess.run(['ip','-j','neigh','show'],capture_output=True,text=True,timeout=2)
            if result.returncode:return []
            rows=json.loads(result.stdout)
            return [{'address':r['dst'],'interface':r['dev'],'mac':r['lladdr'],
                     'state':r.get('state',[])} for r in rows if r.get('lladdr') and r.get('dst') and r.get('dev')][:MAX_NEIGHBORS]
        except (OSError,ValueError,subprocess.SubprocessError):return []

    @staticmethod
    def filesystems():
        rows=[];seen=set()
        for line in read('/proc/mounts').splitlines():
            fields=line.split()
            if len(fields)<3 or not fields[0].startswith('/dev/') or fields[1] in seen:continue
            try:
                mount=fields[1].replace('\\040',' ');usage=os.statvfs(mount);seen.add(fields[1])
                rows.append({'device':fields[0],'mount':mount,'type':fields[2],
                             'total_bytes':usage.f_blocks*usage.f_frsize,'available_bytes':usage.f_bavail*usage.f_frsize})
            except OSError:continue
            if len(rows)>=24:break
        return rows

    def sample(self):
        now=time.monotonic();elapsed=now-self.last;self.last=now
        sampled=time.time();samples=self.hardware.sample(elapsed)
        fields=memory_fields();cached=fields.get('Cached',0)+fields.get('SReclaimable',0)
        memory={'backend_tier':self.hardware.tier,'vendor':self.hardware.vendor,
                'total_bytes':fields.get('MemTotal'),'available_bytes':fields.get('MemAvailable'),
                'cached_bytes':cached,'buffers_bytes':fields.get('Buffers'),'dirty_bytes':fields.get('Dirty'),
                'writeback_bytes':fields.get('Writeback'),'swap_total_bytes':fields.get('SwapTotal'),
                'swap_free_bytes':fields.get('SwapFree'),'pressure_avg10':pressure(),
                'cache':cache_rates(samples),'mbm':mbm_rates(samples),'dram_read_bytes_per_second':None,
                'dram_write_bytes_per_second':None,'agents':{},
                'counters':[{'name':e.label,'value':v,'unit':e.unit} for e,v in samples[:128]]}
        for direction,aliases in [('read',{'cas_count_read','data_reads','data_read'}),('write',{'cas_count_write','data_writes','data_write'})]:
            values=[value for event,value in samples if event.tier in (1,2) and event.unit=='B/s' and event.label.rsplit('/',1)[-1] in aliases]
            if values and all(v is not None for v in values):memory['dram_'+direction+'_bytes_per_second']=sum(values)
        for agent in ('ia','gt','io'):
            values=[value for event,value in samples if event.unit=='requests/s' and event.label.endswith('/'+agent+'_requests')]
            memory['agents'][agent+'_requests_per_second']=sum(values) if values and all(v is not None for v in values) else None
        processes,count,denied=self.processes(elapsed)
        connections,socket_error=self.sockets(elapsed)
        gpu={}
        if not self.gpu_attempted:
            self.gpu_attempted=True
            if Path('/sys/bus/event_source/devices/i915').exists():
                try:self.gpu=GpuMonitor()
                except (OSError,ValueError):pass
        if self.gpu:
            try:
                value=self.gpu.sample()
                gpu={'rates':value['rates'],'sensors':value['sensors'][:24],'notes':value['notes'][:8],
                     'clients':[{'device':r['key'][0],'client':r['key'][1],
                                 'owners':[{'pid':p,'name':n[:80]} for p,n in sorted(r['owners'])[:8]],
                                 'metrics':r['metrics'],'engines':r['engines']} for r in value['clients'][:24]],
                     'denied':value['denied']}
            except (OSError,ValueError):gpu={'status':'unavailable'}
        notes=self.hardware.notes[:6]+([socket_error] if socket_error else [])
        value={'schema_version':SCHEMA_VERSION,'sampled_at':sampled,'status':'live',
               'scope':'Local visible kernel counters; process/owner coverage follows agent PID namespace and permissions. TCP rates are payload, not wire bytes. LAN cache is not proof of reachability.',
               'memory':memory,'processes':processes,'connections':connections,'lan':self.neighbors(),
               'filesystems':self.filesystems(),'gpu':gpu,'notes':notes,
               'limits':{'visible_processes':count,'denied_processes':denied,'process_rows':MAX_PROCESSES,
                         'connection_rows':MAX_CONNECTIONS,'neighbor_rows':MAX_NEIGHBORS,
                         'address_sampling':'on-demand only; no automatic profiling','lan_discovery':'passive neighbor cache; no scans'}}
        # Keep the authenticated transport and receiver below their existing batch budget.
        while len(json.dumps(value,allow_nan=False))>95000:
            biggest=max(('processes','connections','lan'),key=lambda key:len(value[key]))
            if not value[biggest]:raise ValueError('Aggregate telemetry exceeds its payload budget')
            value[biggest].pop()
        return value

    def close(self):
        self.hardware.close()
        if self.gpu:self.gpu.close()
