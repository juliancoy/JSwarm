"""Compare verified file reads on existing Python and native mounts on one host.

This is a transport microbenchmark, not a genomic throughput measurement.
Both first-read and immediate warm-read timings are reported with cache deltas.
"""
import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path
import time


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--previous',type=Path,required=True)
    parser.add_argument('--native',type=Path,required=True)
    parser.add_argument('--report',type=Path,required=True)
    parser.add_argument('--node',default='compai.local')
    args=parser.parse_args()
    plans=[json.loads(p.read_text()) for p in [args.previous,args.native]]
    if plans[0]['object_id']!=plans[1]['object_id']:raise ValueError('Objects differ')
    manifest=json.loads(Path(plans[0]['publication']).read_text())['object']
    page=manifest['page_bytes'];length=8*1024*1024;offset=(manifest['bytes']*5//6)//page*page
    if offset+length>manifest['bytes']:raise ValueError('Object is too small for this comparison')
    rows=[]
    for label,plan in zip(['python','native'],plans):
        record=plan['nodes'][args.node];before=json.loads(Path(record['metrics']).read_text())
        timings=[]
        with open(record['path'],'rb',buffering=0) as f:
            for state in ['first_read','warm_read']:
                started=time.monotonic();data=os.pread(f.fileno(),length,offset);elapsed=time.monotonic()-started
                assert len(data)==length
                for start in range(0,length,page):
                    assert hashlib.sha256(data[start:start+page]).hexdigest()==manifest['pages'][(offset+start)//page]
                timings.append({'state':state,'seconds':elapsed,'mib_per_second':length/1048576/elapsed})
        after=json.loads(Path(record['metrics']).read_text())
        rows.append({'backend':label,'revision':record['revision'],'timings':timings,'before':before,'after':after})
    result={'date':datetime.datetime.now(datetime.timezone.utc).isoformat(),'node':args.node,'bytes':length,'offset':offset,
            'object':manifest['id'],'measurement':'verified transport reads; concurrent fleet workload; first read is not guaranteed cold','results':rows}
    args.report.write_text(json.dumps(result,indent=2));print(json.dumps(result,indent=2))

if __name__=='__main__':main()
