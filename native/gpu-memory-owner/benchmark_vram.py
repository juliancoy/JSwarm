"""Admitted isolated device-resident Vulkan benchmark with GPU-only timings."""
import json
import os
from pathlib import Path
import statistics
import subprocess
import sys
import uuid

ROOT=Path(__file__).resolve().parents[3]
BASE=Path(__file__).resolve().parent
sys.path[:0]=[str(ROOT/'JSwarm'),str(ROOT/'JSwarm/swarm-control/server')]
from agent_client import AgentClient,CONFIG
from scripts.launcher_discovery import resolve_endpoint


def main():
    config=json.loads(CONFIG.read_text());name=config['name'];endpoint,_=resolve_endpoint(name)
    agent=AgentClient({'host':name,'agent_endpoint':endpoint,'local':True});lease=uuid.uuid4().hex
    gpu_query=['nvidia-smi','--query-gpu=name,driver_version,memory.total,memory.free,clocks.current.graphics,clocks.current.memory,utilization.gpu','--format=csv,noheader']
    result={'scope':'Standalone device-only Vulkan timing; no production service changes; 256 MiB reference plus 256 MiB I/O in DEVICE_LOCAL; all setup and correctness readbacks excluded from timed regions',
            'metric_warning':'Read+write GB/s for sequential copy; gather bandwidth is requested logical bytes (index+reference+result), NOT measured DRAM transactions; random access can fetch larger cache sectors; no hardware stall counters collected',
            'gpu_before':subprocess.check_output(gpu_query,text=True).strip(),'load_before':os.getloadavg(),
            'rows':[]}
    try:
        admission=agent.call('resource.acquire',{'id':lease,'group':'gpu-vram-resident-benchmark','cpus':1,'memory_mb':1024,'container':'swarm-job-'+lease,'sink':'gpu','priority':-100},timeout=15)
        if not admission.get('acquired'):raise RuntimeError('No capacity for bounded VRAM benchmark')
        command=[str(BASE/'vram-benchmark'),str(BASE/'gather.spv'),str(BASE/'resident_setup.spv')]
        native=subprocess.run(command,capture_output=True,text=True,timeout=90)
        result['command']=command;result['exit_code']=native.returncode
        (BASE/'vram-native.stdout').write_text(native.stdout)
        (BASE/'vram-native.stderr').write_text(native.stderr)
        if native.returncode:raise RuntimeError(native.stderr)
        lines=[json.loads(line) for line in native.stdout.splitlines()]
        result['device']=lines[0]
        assert lines[0]['device_allocation_bytes']>=512*1024**2
        assert lines[0]['timed_host_payload_copies']==0
        assert lines[0]['timed_host_roundtrips_between_dispatches']==0
        for row in lines[1:]:
            assert row['verified'] and len(row['gpu_seconds'])==5 and all(t>0 for t in row['gpu_seconds'])
            elapsed=statistics.median(row['gpu_seconds']);iterations=row['repetitions']
            row['gpu_us_per_dispatch']=elapsed/iterations*1e6
            row['logical_gb_per_s']=row['logical_bytes_per_iteration']*iterations/elapsed/1e9
            row['lookups_per_s']=None if row['case'].endswith('copy') else row['count']*iterations/elapsed
            result['rows'].append(row);print(json.dumps(row),flush=True)
        assert len(result['rows'])==8
        result['gpu_after']=subprocess.check_output(gpu_query,text=True).strip();result['load_after']=os.getloadavg()
        (BASE/'benchmark-results.vram.json').write_text(json.dumps(result,indent=2)+'\n')
    finally:
        try:agent.call('resource.release',{'id':lease},timeout=15)
        finally:agent.close()

if __name__=='__main__':main()
