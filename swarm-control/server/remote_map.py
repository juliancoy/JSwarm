"""Inspect a connected LAN through a trusted SSH peer, without changing routes."""
import datetime
import json
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).with_name('measurements.py').read_text() + r'''
import concurrent.futures,ipaddress,json,socket,subprocess,time
def output(args):return subprocess.check_output(args,text=True)
routes=json.loads(output(['ip','-j','-4','route']))
default=next(r for r in routes if r['dst']=='default')
iface=default['dev'];gateway=default.get('gateway')
addresses=json.loads(output(['ip','-j','-4','addr','show','dev',iface]))
address=next(a for a in addresses[0]['addr_info'] if a['scope']=='global')
local=address['local'];subnet=ipaddress.ip_network(local+'/'+str(address['prefixlen']),strict=False)
if subnet.prefixlen<24:raise ValueError('Remote map limited to /24 or smaller')
def ping(ip):
 p=subprocess.run(['ping','-n','-I',iface,'-c','1','-W','1',str(ip)],capture_output=True,text=True)
 return str(ip) if p.returncode==0 else None
with concurrent.futures.ThreadPoolExecutor(max_workers=32) as pool:
 alive=set(pool.map(ping,[ip for ip in subnet.hosts() if str(ip)!=local]))
peers=json.loads(output(['ip','-j','-4','neigh','show','dev',iface]))
devices=[]
for peer in peers:
 if not peer.get('lladdr'):continue
 ip=peer['dst'];ports=[]
 for port in (22,80,443):
  try:
   with socket.create_connection((ip,port),timeout=.4):ports.append(port)
  except OSError:pass
 devices.append({'ip':ip,'mac':peer['lladdr'],'ports':ports,'neighbor':peer.get('state',[]),'ping':ip in alive,'self':False})
internet=subprocess.run(['ping','-n','-c','1','-W','2','1.1.1.1'],capture_output=True,text=True)
start=time.monotonic();before=[int(open('/sys/class/net/'+iface+'/statistics/'+d+'_bytes').read()) for d in ('rx','tx')];time.sleep(.5);after=[int(open('/sys/class/net/'+iface+'/statistics/'+d+'_bytes').read()) for d in ('rx','tx')];elapsed=time.monotonic()-start
traffic={'interface':iface,'rx_mbps':max(0,(after[0]-before[0])*8/elapsed/1e6),'tx_mbps':max(0,(after[1]-before[1])*8/elapsed/1e6),'sample_seconds':elapsed}
print(json.dumps({'link':link_info(iface),'route':route_trace(),'traffic':traffic,'hostname':socket.gethostname(),'interface':iface,'local_ip':local,'subnet':str(subnet),'gateway':gateway,'dns':output(['cat','/etc/resolv.conf']),'devices':devices,'internet_probe':{'target':'1.1.1.1','success':internet.returncode==0,'output':internet.stdout.strip()}}))
'''


def observe(device, interface):
    address = device.get('ssh_ip', device['ip'])
    destination = address + ('%' + interface if ':' in address else '')
    errors = []
    for user in ('julian', 'wolf'):
        try:
            result = subprocess.run(['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=4',
                                     '-o', 'StrictHostKeyChecking=yes', user+'@'+destination,
                                     'python3 -'], input=SCRIPT, capture_output=True, text=True, timeout=45)
            if result.returncode == 0:
                data = json.loads(result.stdout.strip().splitlines()[-1])
                data.update(status='measured', via_mac=device['mac'],
                            observed=datetime.datetime.now(datetime.timezone.utc).isoformat())
                data['traffic']['updated'] = data['observed']
                return data
            errors.append(result.stderr.strip()[-200:])
        except Exception as exc:
            errors.append(str(exc))
    return {'status': 'unavailable', 'error': '; '.join(errors),
            'observed': datetime.datetime.now(datetime.timezone.utc).isoformat()}


def collect(devices, interface):
    import concurrent.futures
    candidates = [d for d in devices if not d['self'] and 22 in d['ports']]
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda device: observe(device, interface), candidates))
    return {(device.get('mac') or device['ip']): result
            for device, result in zip(candidates, results)}
