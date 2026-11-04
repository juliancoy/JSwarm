#!/usr/bin/env python3
"""Bounded, authenticated public-object prefix benchmark; never copies BAMs.

Uses the enrolled worker's pinned mTLS and signed object allowlist. The supplied
expected SHA256 must come from the source prefix. No private keys leave a node.
Reads use one reusable buffer; results describe warm-cache TLS throughput, not
RDMA or cold-disk bandwidth. Does not alter the worker or restart any service.
"""
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import resource
import subprocess
import time


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',type=Path,required=True)
    parser.add_argument('--peer',required=True)
    parser.add_argument('--object',required=True)
    parser.add_argument('--sha256',required=True)
    parser.add_argument('--bytes',type=int,default=32*1024*1024)
    args=parser.parse_args()
    if not 0<args.bytes<=64*1024*1024:parser.error('Prefix size must be at most 64 MiB')
    module_spec=importlib.util.spec_from_file_location('enrolled_worker',args.root/'worker.py')
    worker=importlib.util.module_from_spec(module_spec);module_spec.loader.exec_module(worker)
    peer=worker.Peer(args.root)
    if args.object not in peer.objects:parser.error('Object not in signed manifest')
    address=peer.manifest['nodes'][args.peer]['address']
    route=subprocess.run(['ip','route','get',address],capture_output=True,text=True).stdout.strip()
    measurements=[]
    for size in [64*1024,256*1024,1024*1024]:
        buffer=bytearray(size);view=memoryview(buffer);digest=hashlib.sha256();received=0
        cpu=time.process_time();start=time.monotonic()
        with peer.open_peer(args.peer,'/objects/'+args.object,timeout=30,headers={'Range':'bytes=0-'}) as response:
            while received<args.bytes:
                count=response.readinto(view[:min(size,args.bytes-received)])
                if not count:raise RuntimeError('Incomplete prefix')
                digest.update(view[:count]);received+=count
        elapsed=time.monotonic()-start;cpu=time.process_time()-cpu
        if digest.hexdigest()!=args.sha256:raise ValueError('Source prefix SHA256 mismatch')
        measurements.append({'buffer_bytes':size,'bytes':received,'seconds':round(elapsed,4),'Mbps':round(received*8/elapsed/1e6,2),'receiver_cpu_seconds':round(cpu,4),'prefix_sha256_verified':True})
    print(json.dumps({'node':peer.name,'peer':args.peer,'route':route,'transport':'Pinned mutual TLS 1.3; ordinary NIC DMA, no RDMA','scope':'Public VCF prefix; warm-cache sample; SHA256 verified against source','peak_rss_KiB':resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,'measurements':measurements},indent=2))


if __name__=='__main__':main()
