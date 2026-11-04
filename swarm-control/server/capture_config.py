"""Publish host-derived capture interfaces and local prefixes to the restricted collector."""
from paths import STATE
import ipaddress
import json
from pathlib import Path
import subprocess
import time


def configuration(lan):
    def ip(*args):
        return json.loads(subprocess.check_output(['ip', '-j', *args], text=True, timeout=4))
    addresses = ip('addr')
    routes = ip('-4', 'route', 'show', 'table', 'main') + ip('-6', 'route', 'show', 'table', 'main')
    names = {lan} | {r['dev'] for r in routes if r.get('dst') == 'default' and 'dev' in r}
    networks, host_addresses, interfaces = set(), [], {}
    for row in addresses:
        prefixes = []
        for address in row.get('addr_info', []):
            value = address['local']
            host_addresses.append(value)
            net = str(ipaddress.ip_network(value+'/'+str(address['prefixlen']), strict=False))
            networks.add(net); prefixes.append(net)
        if row['ifname'] in names and row.get('link_type') == 'ether':
            interfaces[row['ifname']] = {'mac':row['address'], 'networks':prefixes}
    # Directly routed/delegated prefixes can be global IPv6 yet still local.
    for route in routes:
        if route.get('dst') != 'default' and not route.get('gateway') and route.get('type', 'unicast') == 'unicast':
            net = str(ipaddress.ip_network(route['dst'], strict=False))
            networks.add(net)
            if route.get('dev') in interfaces and net not in interfaces[route['dev']]['networks']:
                interfaces[route['dev']]['networks'].append(net)
    return {'lan_interface':lan, 'host_mac':interfaces[lan]['mac'], 'interfaces':interfaces,
            'host_addresses':host_addresses, 'local_networks':sorted(networks), 'updated_epoch':time.time()}


def publish(lan):
    directory = STATE
    temporary = directory / 'capture-config.tmp'
    temporary.write_text(json.dumps(configuration(lan)))
    temporary.replace(directory / 'capture-config.json')
