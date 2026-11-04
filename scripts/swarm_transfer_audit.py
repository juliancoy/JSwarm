#!/usr/bin/env python3
"""Read-only Linux NIC, DMA capability, storage and memory-pressure inventory.

DMA masks describe addressability, not measured DMA traffic. Driver-managed NIC
DMA is distinct from RDMA, which needs a separate device and transport.
"""
import ctypes
import json
from pathlib import Path
import socket
import time


class Value(ctypes.Structure):
    _fields_=[('cmd',ctypes.c_uint32),('data',ctypes.c_uint32)]


class Ifreq(ctypes.Structure):
    _fields_=[('name',ctypes.c_char*16),('data',ctypes.c_void_p),('padding',ctypes.c_char*16)]


def audit():
    libc=ctypes.CDLL(None,use_errno=True)
    rows=[]
    for interface in Path('/sys/class/net').iterdir():
        if not (interface/'device').exists():continue
        row={'interface':interface.name,'driver':(interface/'device/driver').resolve().name}
        for key in ('dma_mask_bits','consistent_dma_mask_bits'):
            path=interface/'device'/key
            if path.exists():row[key]=int(path.read_text())
        for key in ('speed','duplex','operstate'):
            try:row['link_mbps' if key=='speed' else key]=(interface/key).read_text().strip()
            except OSError:pass
        row['offloads']={}
        with socket.socket(socket.AF_INET,socket.SOCK_DGRAM) as sock:
            for name,cmd in {'rx_checksum':0x14,'tx_checksum':0x16,'scatter_gather':0x18,'tcp_segmentation':0x1e,'generic_segmentation':0x23,'generic_receive':0x2b}.items():
                value=Value(cmd,0)
                request=Ifreq(interface.name.encode(),ctypes.addressof(value))
                rc=libc.ioctl(sock.fileno(),0x8946,ctypes.byref(request))
                row['offloads'][name]=bool(value.data) if rc==0 else None
        rows.append(row)
    # Reclaimable page cache is useful; MemAvailable and PSI reflect headroom.
    mem={line.split(':')[0]:int(line.split()[1])*1024 for line in Path('/proc/meminfo').read_text().splitlines() if line.split()[1].isdigit()}
    return {'node':socket.gethostname(),'time':time.time(),'interfaces':rows,
            'rdma_devices':[p.name for p in Path('/sys/class/infiniband').glob('*')],
            'memory':{k:mem.get(k) for k in ['MemTotal','MemAvailable','Cached','SReclaimable','Dirty','Writeback','SwapTotal','SwapFree']},
            'pressure':{k:Path('/proc/pressure',k).read_text().strip() for k in ['cpu','memory','io']},
            'numa_nodes':[p.name for p in Path('/sys/devices/system/node').glob('node[0-9]*')],
            'storage':{p.name:{k:(p/'queue'/k).read_text().strip() for k in ['read_ahead_kb','scheduler','rotational'] if (p/'queue'/k).exists()} for p in Path('/sys/block').iterdir() if not p.name.startswith(('loop','ram'))}}


if __name__=='__main__':
    print(json.dumps(audit(),indent=2))
