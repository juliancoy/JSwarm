"""Bounded in-memory resource samples; no credentials or process/file details."""
from collections import deque
from copy import deepcopy

WINDOW_SECONDS = 300
MAX_SAMPLES = 2048
HOST_FIELDS = 'host observed_hostname addresses status sampled_at window_seconds percent cpus ram_percent ram_used_bytes ram_total_bytes disk_busy_percent disk_basis network_mbps network_tx_mbps network_rx_mbps cpu_iowait_percent cpu_steal_percent cpu_pressure_some_percent'
GPU_FIELDS = 'pci_id vendor name backend utilization_percent utilization_basis utilization_unavailable_reason memory_total_mib memory_free_mib'


def project_interfaces(rows):
    fields='name ifindex state status sampled_at window_seconds rx_mbps tx_mbps network_mbps link_speed_mbps link_rx_mbps link_tx_mbps link_duplex rx_utilization_percent tx_utilization_percent last_link_change_at rx_errors_total rx_errors_delta tx_errors_total tx_errors_delta rx_dropped_total rx_dropped_delta tx_dropped_total tx_dropped_delta rx_crc_errors_total rx_crc_errors_delta carrier_changes_total carrier_changes_delta carrier_down_count_total carrier_down_count_delta carrier_up_count_total carrier_up_count_delta'
    return [{key:deepcopy(row[key]) for key in fields.split() if key in row} for row in rows[:32]]


def project_disks(rows):
    fields='name model identity sampled_at status read_bytes_per_second write_bytes_per_second read_percent write_percent total_percent busy_percent capacity_basis capacity_bytes_per_second'
    return [{key:deepcopy(row[key]) for key in fields.split() if key in row} for row in rows[:32]]


def project_host(row):
    host = {key: deepcopy(row[key]) for key in HOST_FIELDS.split() if key in row}
    host['network_interfaces']=project_interfaces(row.get('network_interfaces',[]))
    host['disks']=project_disks(row.get('disks',[]))
    performance=row.get('performance') or {}
    host['performance']={key:deepcopy(performance[key]) for key in ('schema_version','sampled_at','status','scope','memory') if key in performance}
    if 'memory' in host['performance']:host['performance']['memory'].pop('counters',None)
    hardware = row.get('hardware') or {}
    host['hardware'] = {key: hardware[key] for key in ('stale','sampled_at') if key in hardware}
    host['hardware']['gpus'] = [{key: gpu[key] for key in GPU_FIELDS.split() if key in gpu}
                                for gpu in hardware.get('gpus', [])[:16]]
    return host


def project_network(value):
    def pick(row,fields):return {key:deepcopy(row[key]) for key in fields.split() if key in row}
    capture=value.get('link_traffic') or {}
    return {'interface':value.get('interface'),
            'devices':[pick(row,'hostname ip mac self hostname_source hostname_last_seen') for row in value.get('devices',[])[:256]],
            'interface_traffic':{key:pick(row,'rx_mbps tx_mbps updated') for key,row in list(value.get('interface_traffic',{}).items())[:32]},
            'link_traffic':{'updated':capture.get('updated'),'capture_error':bool(capture.get('capture_error')),
                            'peers':{key:pick(row,'rx_mbps tx_mbps') for key,row in list(capture.get('peers',{}).items())[:256]}}}


class ResourceHistory:
    def __init__(self):
        self.samples = deque(maxlen=MAX_SAMPLES)

    def append(self, hosts, now, network=None, partial=False):
        frame={'sampled_at': now, 'hosts': [project_host(host) for host in hosts[:64]]}
        if partial:frame['partial']=True
        if network is not None:frame['network']=project_network(network)
        frames=[*self.samples,frame]
        frames.sort(key=lambda row:row['sampled_at'])
        latest=frames[-1]['sampled_at']
        self.samples=deque((row for row in frames if row['sampled_at']>=latest-WINDOW_SECONDS),maxlen=MAX_SAMPLES)

    def prune(self, now):
        while self.samples and self.samples[0]['sampled_at'] < now - WINDOW_SECONDS:
            self.samples.popleft()

    def view(self, now):
        self.prune(now)
        return deepcopy(list(self.samples))
