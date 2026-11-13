"""Bounded host integration check for the node's actual mDNS publication path."""
import datetime
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time
import uuid

from scripts.launcher_discovery import Discovery, resolve_endpoint, service_hostname
from scripts.launcher_registry import listener_addresses


def main():
    project=Path(__file__).resolve().parents[1]
    files=[project/'scripts/launcher_discovery.py',project/'scripts/launcher_registry.py',Path(__file__)]
    result={'utc':datetime.datetime.now(datetime.timezone.utc).isoformat(),
            'command':[sys.executable,*sys.argv],
            'fingerprints':{str(p.relative_to(project)):hashlib.sha256(p.read_bytes()).hexdigest() for p in files}}
    discovery=None;started=False
    try:
        rows=json.loads(subprocess.check_output(['ip','-j','-4','addr'],text=True,timeout=5))
        addresses=listener_addresses(rows)
        if not addresses:raise RuntimeError('No UP physical IPv4 interface')
        node='swarm-check-'+uuid.uuid4().hex[:10]+'.local'
        discovery=Discovery(node,{node:addresses[0]},8790,addresses=addresses)
        discovery.start();started=True
        deadline=time.monotonic()+20
        while True:
            try:
                endpoint,resolved=resolve_endpoint(node)
                assert endpoint=='http://'+service_hostname(node)+':8790'
                assert set(resolved).intersection(addresses)
                result.update(result='passed',endpoint=endpoint,resolved_addresses=resolved)
                break
            except OSError:
                if time.monotonic()>=deadline:raise
                time.sleep(1)
    except Exception as error:
        result.update(result='failed',error=repr(error))
        raise
    finally:
        if discovery:
            if started:discovery.close()
            else:discovery.zc.close()
        with (project/'tests/mdns-test-runs.jsonl').open('a') as out:
            out.write(json.dumps(result)+'\n')
        print(json.dumps(result),flush=True)


if __name__=='__main__':main()
