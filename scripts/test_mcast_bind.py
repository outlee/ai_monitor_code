#!/usr/bin/env python3
# ASCII-only multicast join test (CentOS paste-safe)
import socket
import struct
import sys

g = sys.argv[1] if len(sys.argv) > 1 else "239.100.3.1"
iface = sys.argv[2] if len(sys.argv) > 2 else "enp1s0f1"
port = int(sys.argv[3]) if len(sys.argv) > 3 else 5000
ifindex = socket.if_nametoindex(iface)

s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
try:
    s.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, iface.encode("ascii") + b"\0")
    print("BINDTODEVICE ok", iface)
except OSError as e:
    print("BINDTODEVICE fail", e)

s.bind(("", port))
mreqn = struct.pack("4s4si", socket.inet_aton(g), socket.inet_aton("0.0.0.0"), ifindex)
try:
    s.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreqn)
    print("JOIN ok", g, "ifindex", ifindex)
except OSError as e:
    print("JOIN fail", e)
    sys.exit(1)

s.settimeout(8)
try:
    n = total = 0
    while n < 20:
        d = s.recv(2048)
        total += len(d)
        n += 1
    print("OK packets", n, "bytes", total)
except Exception as e:
    print("RECV fail", type(e).__name__, e)
    sys.exit(2)
