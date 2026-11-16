"""Admitted cache grouping A/B: total grouping cost versus reusable ordering."""
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
            'metric_warning':'GPU-only fixed random query batches; group-every-batch includes GPU histogram, prefix, scatter and result reorder. grouped-reuse excludes grouping and is amortized-only. No cache hit or stall counters collected.',
            'gpu_before':subprocess.check_output(gpu_query,text=True).strip(),'load_before':os.getloadavg(),
            'rows':[]}
    try:
        admission=agent.call('resource.acquire',{'id':lease,'group':'gpu-cache-group-benchmark','cpus':1,'memory_mb':1024,'container':'swarm-job-'+lease,'sink':'gpu','priority':-100},timeout=15)
        if not admission.get('acquired'):raise RuntimeError('No capacity for bounded VRAM benchmark')
        command=[str(BASE/'cache-benchmark'),str(BASE/'gather.spv'),str(BASE/'resident_setup.spv'),str(BASE/'cache_group.spv')]
        native=subprocess.run(command,capture_output=True,text=True,timeout=90)
        result['command']=command;result['exit_code']=native.returncode
        (BASE/'cache-native.stdout').write_text(native.stdout)
        (BASE/'cache-native.stderr').write_text(native.stderr)
        if native.returncode:raise RuntimeError(native.stderr)
        lines=[json.loads(line) for line in native.stdout.splitlines()]
        for row in lines:
            assert row['verified'] and len(row['gpu_seconds'])==5 and all(t>0 for t in row['gpu_seconds'])
            elapsed=statistics.median(row['gpu_seconds'])/row['repetitions']
            row['gpu_us_per_batch']=elapsed*1e6
            row['lookups_per_s']=row['count']/elapsed
            result['rows'].append(row);print(json.dumps(row),flush=True)
        assert len(result['rows'])==24
        result['gpu_after']=subprocess.check_output(gpu_query,text=True).strip();result['load_after']=os.getloadavg()
        (BASE/'benchmark-results.cache.json').write_text(json.dumps(result,indent=2)+'\n')
    finally:
        try:agent.call('resource.release',{'id':lease},timeout=15)
        finally:agent.close()

if __name__=='__main__':main()
