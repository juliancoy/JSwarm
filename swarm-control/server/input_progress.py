"""Read-only input scan telemetry for the existing, unchanged QC containers."""
import hashlib
import json
from pathlib import Path
import subprocess
import time


def snapshot():
    results=[]
    ids=subprocess.check_output(['docker','ps','-q','--filter','label=org.lifetech.venter-peer'],text=True,timeout=10).split()
    if not ids:return results
    containers=json.loads(subprocess.check_output(['docker','inspect',*ids],text=True,timeout=10))
    for container in containers:
        try:
            run=container['Config']['Labels']['org.lifetech.venter-peer']
            root=Path.home()/'.local/state/venter-peer-qc'/run
            manifest=json.loads((root/'manifest.json').read_text())['value']
            name=container['Name'].removeprefix('/')
            job=next(j for j in manifest['jobs'] if name=='venter-qc-'+hashlib.sha256(run.encode()).hexdigest()[:8]+'-'+j['id'][:16])
            command=[container['Path'],*container['Args']]
            # Quickcheck can seek to EOF: it is never evidence of a scanned input.
            if job['spec']['kind']=='bam-read-qc' and command[:2] not in (['samtools','stats'],['python3','-c']):continue
            if job['spec']['kind']=='variant-summary' and command[:2]!=['bcftools','stats']:continue
            if job['spec']['kind']=='fasta-summary' and command[:2]!=['python3','-c']:continue
            mount=next(m for m in container['Mounts'] if m['Destination']=='/input' and not m['RW'])
            stat=Path(mount['Source']).stat()
            if stat.st_size!=job['spec']['bytes']:continue
            pid=container['State']['Pid'];proc=Path('/proc')/str(pid)
            positions=[]
            for fd in (proc/'fd').iterdir():
                try:
                    if str(fd.readlink())!='/input':continue
                    fields=dict(line.split(':',1) for line in (proc/'fdinfo'/fd.name).read_text().splitlines() if ':' in line)
                    if int(fields['ino'])==stat.st_ino:positions.append(int(fields['pos']))
                except (OSError,ValueError,KeyError):continue
            if not positions:continue
            results.append({'run':run,'job':job['id'],'sampled_at':time.time(),'container':container['Id'],
                            'completed_units':min(max(positions),stat.st_size),'total_units':stat.st_size,
                            'unit':'input bytes','basis':'Sequential input read position; compressed bytes where applicable. Read-ahead can precede processing; result validation is separate.'})
        except (OSError,ValueError,KeyError,StopIteration):continue
    return results

if __name__=='__main__':print(json.dumps(snapshot()))
