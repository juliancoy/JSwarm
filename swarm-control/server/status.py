"""Bounded, read-only projection of the local coordinator for paired owners."""
from resource_history import project_host, project_network, project_interfaces, project_disks, WINDOW_SECONDS, MAX_SAMPLES
import json
import os
from pathlib import Path
from urllib.request import Request, urlopen

# Coordinator snapshots include bounded flow and five-minute telemetry histories.
# Keep a hard read bound above their normal combined size; public responses still
# use the field/count projections below.
MAX_COORDINATOR_SNAPSHOT_BYTES = 16 * 1024 * 1024

def request_tests():
    """Queue the complete application-owned catalog; no caller-supplied commands."""
    from master_api import TOKEN_FILE
    token=Path(os.getenv('MAP_MASTER_TOKEN_FILE',str(TOKEN_FILE))).read_text().strip()
    headers={'Authorization':'Bearer '+token,'Content-Type':'application/json'}
    base='http://127.0.0.1:8089/api/v1/master'
    with urlopen(Request(base+'/suites',headers=headers),timeout=10) as response:
        suites=sorted({item['id'].split('--',1)[0] for item in json.load(response)['suites']})
    request=Request(base+'/test-requests',headers=headers,data=json.dumps({'suites':suites,'force':False}).encode())
    with urlopen(request,timeout=10) as response:return json.load(response)


def read_coordinator(path):
    # Callers select fixed paths; never forward a browser-supplied destination.
    with urlopen(Request('http://127.0.0.1:8089' + path), timeout=4) as response:
        raw = response.read(MAX_COORDINATOR_SNAPSHOT_BYTES + 1)
    if len(raw) > MAX_COORDINATOR_SNAPSHOT_BYTES:
        raise ValueError('Coordinator snapshot exceeded the size limit')
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError('Invalid coordinator snapshot')
    return value


def snapshot():
    return project_status(read_coordinator('/api/devices'), read_coordinator('/api/distributed-tests'))


def project_status(network, tests):
    run = tests.get('run') or {}
    active=tests.get('active_runs',[])
    if active:
        runs=[]
        for item in active[:8]:
            runs.append((tests if item['id']==run.get('id') else read('/api/distributed-tests?id='+item['id'])).get('run') or {})
        merged={}
        for current in runs:
            for host in current.get('hosts',[]):
                name=host['host'];entry=merged.setdefault(name,{**host,'jobs':{}})
                if host.get('status') in ('running','preparing','queued'):entry['status']=host['status']
                for key,job in host.get('jobs',{}).items():
                    entry['jobs'][current['id']+'-'+key]={**job,'name':job.get('name',key)+' · '+current['id'][-8:]}
        run={**run,'status':'running','hosts':list(merged.values())}

    interfaces = []
    for name, traffic in list(network.get('interface_traffic', {}).items())[:16]:
        link = network.get('links', {}).get(name, {})
        interfaces.append({'name': name, 'rx_mbps': traffic.get('rx_mbps'),
                           'tx_mbps': traffic.get('tx_mbps'), 'updated': traffic.get('updated'),
                           'capacity_mbps': link.get('capacity_mbps')})
    return {
        'network': {'interface': network.get('interface'), 'subnet': network.get('subnet'),
                    'gateway': network.get('gateway'), 'interfaces': interfaces},
        'active_runs':active,
        'updated': network.get('updated'), 'scanning': bool(network.get('scanning')),
        'devices': [{'hostname': device.get('hostname') or device.get('ip'),
                     'address': device.get('ip'), 'reachable': device.get('ping') is True,
                     'coordinator': bool(device.get('self'))}
                    for device in network.get('devices', [])[:256]],
        'run': {'id': run.get('id'), 'status': run.get('status', 'No run recorded'),
                'started': run.get('started'), 'finished': run.get('finished'),
                'hosts': [{'host': host.get('host'), 'status': host.get('status'),
                           'address': host.get('lan_address'), 'cpus': host.get('cpus'),
                           'memory_mb': host.get('capacity', {}).get('memory_mb'),
                           'jobs': [{'name': job.get('name') or key, 'status': job.get('status')}
                                    for key, job in list(host.get('jobs', {}).items())[:100]]}
                          for host in run.get('hosts', [])[:64]]}}


def read_resources():
    """Fixed local monitor destination; identity checks remain at the owner API."""
    with urlopen(Request('http://127.0.0.1:8795/api/resources'), timeout=4) as response:
        raw=response.read(MAX_COORDINATOR_SNAPSHOT_BYTES+1)
    if len(raw)>MAX_COORDINATOR_SNAPSHOT_BYTES:raise ValueError('Resource snapshot exceeded size limit')
    value=json.loads(raw)
    if not isinstance(value,dict) or not isinstance(value.get('hosts'),list):raise ValueError('Invalid resource snapshot')
    # The resource service already bounds details, omits argv and projects history.
    return {key:value[key] for key in ('hosts','history','history_window_seconds') if key in value}


def task_manager():
    """Resource measurements for the same authenticated owner on any network."""
    network = read_coordinator('/api/devices?resolve=0')
    metrics = read_coordinator('/api/cpu')
    tests = read_coordinator('/api/distributed-tests')
    def pick(value, fields):
        return {key: value[key] for key in fields.split() if key in value}
    data = pick(network, 'updated interface')
    data['devices'] = [pick(row, 'hostname ip mac self hostname_source hostname_last_seen') for row in network.get('devices', [])[:256]]
    data['interface_traffic'] = {name: pick(row, 'rx_mbps tx_mbps updated')
                               for name, row in list(network.get('interface_traffic', {}).items())[:32]}
    capture = network.get('link_traffic') or {}
    data['link_traffic'] = pick(capture, 'updated capture_error')
    data['link_traffic']['peers'] = {name: pick(row, 'rx_mbps tx_mbps')
                                   for name, row in list(capture.get('peers', {}).items())[:256]}
    feed = network.get('docker_downloads') or {}
    data['docker_downloads'] = pick(feed, 'status sampled_at engine_status')
    for kind in ('active', 'recent'):
        data['docker_downloads'][kind] = [pick(row, 'image pid observed_at evidence') for row in feed.get(kind, [])[:16]]
    summary = pick(metrics, 'total_percent live_hosts total_hosts measured_logical_cpus activity_status test_hosts active_test_percent measured_test_hosts')
    summary['hosts'] = []
    for row in metrics.get('hosts', [])[:64]:
        host = pick(row, 'host observed_hostname addresses status sampled_at sample_age_seconds window_seconds percent cpus ram_percent ram_used_bytes ram_total_bytes disk_busy_percent disk_basis network_mbps network_tx_mbps network_rx_mbps cpu_iowait_percent cpu_steal_percent cpu_pressure_some_percent running_test_jobs scheduled_cpu_slots workload_phase')
        hardware = row.get('hardware') or {}
        host['network_interfaces']=project_interfaces(row.get('network_interfaces',[]))
        host['disks']=project_disks(row.get('disks',[]))
        host['hardware'] = pick(hardware, 'stale sampled_at')
        host['hardware']['gpus'] = [pick(gpu, 'pci_id vendor name backend utilization_percent utilization_unavailable_reason memory_total_mib memory_free_mib') for gpu in hardware.get('gpus', [])[:16]]
        summary['hosts'].append(host)
    # Reapply the same field allowlist at the authenticated boundary.
    summary['history'] = [{'sampled_at': frame['sampled_at'], 'partial': frame.get('partial') is True, 'hosts': [project_host(host) for host in frame.get('hosts', [])[:64]],
                           **({'network':project_network(frame['network'])} if 'network' in frame else {})}
                          for frame in metrics.get('history', [])[-MAX_SAMPLES:]]
    summary['history_window_seconds'] = WINDOW_SECONDS
    run = project_status(network, tests)['run']
    progress = (tests.get('run') or {}).get('image_progress')
    if isinstance(progress, dict):
        run['image_progress'] = pick(progress, 'image phase source destination updated transfer_phase eta_seconds bytes_total bytes_completed bytes_per_second')
    try:resources=read_resources()
    except (OSError,ValueError):resources={'hosts':[],'history':[],'status':'unavailable'}
    return {'network': data, 'metrics': summary, 'resources': resources, 'run': run}


def research_jobs(body=None):
    from master_api import TOKEN_FILE
    token=Path(os.getenv('MAP_MASTER_TOKEN_FILE',str(TOKEN_FILE))).read_text().strip()
    headers={'Authorization':'Bearer '+token,'Content-Type':'application/json'}
    base='http://127.0.0.1:8089/api/v1/master'
    def call(path,value=None):
        with urlopen(Request(base+path,headers=headers,data=json.dumps(value).encode() if value is not None else None),timeout=10) as response:return json.load(response)
    if body is None:
        records=call('/test-requests')['requests']
        jobs=[]
        for record in records[:30]:
            row={k:record.get(k) for k in ('id','kind','status','submitted_at','run_id','finished_at')}
            if record.get('kind')=='research' and record.get('run_id'):
                run=call('/runs/'+record['run_id']).get('run') or {}
                row['progress']=run.get('progress')
            jobs.append(row)
        return {'profiles':call('/research-profiles')['profiles'],'jobs':jobs}
    if not isinstance(body,dict):raise ValueError('Invalid job action')
    if set(body)=={'profile','request_id'}:return call('/research-requests',body)
    if set(body)=={'cancel'}:return call('/cancel-request',{'request_id':body['cancel']})
    raise ValueError('Invalid job action')


def connections():
    """Small authenticated connection feed, independent of resource sampling."""
    value=read_coordinator('/api/connections')
    fields=('host','address','status','checked_at','last_seen','connected_at','ready','accepting_jobs')
    return {**{key:value[key] for key in ('sampled_at','poll_interval_seconds','heartbeat_timeout_seconds','stale_after_seconds','inventory_available') if key in value},
            'hosts':[{key:row[key] for key in fields if key in row} for row in value.get('hosts',[])[:64]]}
