#!/usr/bin/python3
"""Advertise map.local for the shared Ethernet address."""
import json
import socket
import struct
import subprocess
import sys

import dbus
from dbus.mainloop.glib import DBusGMainLoop
from gi.repository import GLib

DBusGMainLoop(set_as_default=True)
interface = 'enp7s0'
links = json.loads(subprocess.check_output(['ip', '-j', '-4', 'addr', 'show', 'dev', interface]))
address = next(a['local'] for a in links[0]['addr_info'] if a['scope'] == 'global')
index = int(links[0]['ifindex'])
bus = dbus.SystemBus()
server = dbus.Interface(bus.get_object('org.freedesktop.Avahi', '/'), 'org.freedesktop.Avahi.Server')
group = dbus.Interface(bus.get_object('org.freedesktop.Avahi', server.EntryGroupNew()),
                       'org.freedesktop.Avahi.EntryGroup')
loop = GLib.MainLoop()
failed = False


def changed(state, error):
    global failed
    print(f'mDNS entry state: {state} {error}', flush=True)
    if state in (3, 4):
        print('mDNS publication failed: ' + str(error), file=sys.stderr, flush=True)
        failed = True
        loop.quit()


group.connect_to_signal('StateChanged', changed)
# AVAHI_PUBLISH_NO_REVERSE: keep compai.local's existing reverse PTR record.
group.AddAddress(dbus.Int32(index), dbus.Int32(-1), dbus.UInt32(16), 'map.local', address)
group.Commit()
# Some Wi-Fi multicast paths drop IPv4 answers. Reply directly to queries received
# on Ethernet as well; the answer is limited to our own forward-only map.local A RR.
ipv4 = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
ipv4.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
ipv4.bind(('', 5353))
ipv4.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP,
                socket.inet_aton('224.0.0.251') + socket.inet_aton(address))
ipv4.setsockopt(socket.IPPROTO_IP, 8, 1)  # IP_PKTINFO
ipv4.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 255)
ipv4.setblocking(False)


def dns_name(packet, offset, depth=0):
    if depth > 8:
        raise ValueError('DNS pointer loop')
    labels = []
    while packet[offset]:
        length = packet[offset]
        if length & 0xc0 == 0xc0:
            target = ((length & 0x3f) << 8) | packet[offset+1]
            suffix, _ = dns_name(packet, target, depth+1)
            return '.'.join(labels + [suffix]), offset+2
        if length > 63:
            raise ValueError('Invalid label')
        offset += 1
        labels.append(packet[offset:offset+length].decode('ascii').lower())
        offset += length
    return '.'.join(labels), offset+1


def answer_ipv4(sock, condition):
    try:
        packet, ancillary, _, peer = sock.recvmsg(9000, 256)
        received = next((struct.unpack('I', value[:4])[0] for level, kind, value in ancillary
                         if level == socket.IPPROTO_IP and kind == 8), None)
        if received != index or len(packet) < 12 or packet[2] & 0x80:
            return True
        offset = 12
        for _ in range(min(struct.unpack('!H', packet[4:6])[0], 20)):
            name, offset = dns_name(packet, offset)
            kind, category = struct.unpack('!HH', packet[offset:offset+4])
            offset += 4
            if name == 'map.local' and kind in (1, 255) and category & 0x7fff == 1:
                record = b'\x03map\x05local\0' + struct.pack('!HHIH', 1, 0x8001, 120, 4) + socket.inet_aton(address)
                query_id = struct.unpack('!H', packet[:2])[0] if peer[1] != 5353 else 0
                sock.sendto(struct.pack('!6H', query_id, 0x8400, 0, 1, 0, 0) + record, peer)
                break
    except (OSError, ValueError, IndexError, UnicodeError, struct.error):
        pass
    return True


GLib.io_add_watch(ipv4, GLib.IO_IN, answer_ipv4)
print(f'Publishing map.local → {address} on {interface}', flush=True)
try:
    loop.run()
finally:
    group.Free()
sys.exit(1 if failed else 0)
