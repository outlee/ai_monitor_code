#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Multicast interface relay for CentOS hosts where FFmpeg localaddr/IGMP UDP
join fails, but tcpdump can still see packets on the business NIC.

Method: AF_PACKET (like tcpdump) on a named interface, filter IPv4 UDP to
a multicast group:port, forward payload to 127.0.0.1:local_port.

Then point ai_monitor channel url to: udp://127.0.0.1:LOCAL_PORT

Example:
  python3 scripts/mcast_iface_relay.py \\
    --iface enp1s0f1 \\
    --group 239.100.3.1 \\
    --port 5000 \\
    --local-port 15501

Requires: root (packet socket). Python3 stdlib only.
Compatible with CentOS 7 Python 3.6.
"""

import argparse
import socket
import struct
import sys
import time

try:
    from typing import Optional
except ImportError:
    Optional = None  # type: ignore


ETH_P_ALL = 0x0003
ETH_P_IP = 0x0800


def mac_for_ipv4_mcast(ip):
    """Ethernet MAC for IPv4 multicast: 01:00:5e + lower 23 bits of IP."""
    parts = [int(x) for x in ip.split(".")]
    ip_int = (parts[0] << 24) | (parts[1] << 16) | (parts[2] << 8) | parts[3]
    body = ip_int & 0x7FFFFF
    return bytes(
        [
            0x01,
            0x00,
            0x5E,
            (body >> 16) & 0xFF,
            (body >> 8) & 0xFF,
            body & 0xFF,
        ]
    )


def parse_ipv4_udp_payload(frame, group, udp_port):
    if len(frame) < 14:
        return None
    ethertype = struct.unpack("!H", frame[12:14])[0]
    off = 14
    # Optional 802.1Q
    if ethertype == 0x8100:
        if len(frame) < 18:
            return None
        ethertype = struct.unpack("!H", frame[16:18])[0]
        off = 18
    if ethertype != ETH_P_IP:
        return None
    ip = frame[off:]
    if len(ip) < 20:
        return None
    vihl = ip[0]
    version, ihl = vihl >> 4, (vihl & 0x0F) * 4
    if version != 4 or len(ip) < ihl + 8:
        return None
    if ip[9] != 17:  # UDP
        return None
    dst = socket.inet_ntoa(ip[16:20])
    if dst != group:
        return None
    udp = ip[ihl:]
    if len(udp) < 8:
        return None
    dport = struct.unpack("!H", udp[2:4])[0]
    if dport != udp_port:
        return None
    # UDP length includes header
    ulen = struct.unpack("!H", udp[4:6])[0]
    payload = udp[8:ulen] if ulen >= 8 else udp[8:]
    return payload if payload else None


def main():
    ap = argparse.ArgumentParser(description="AF_PACKET multicast -> localhost UDP relay")
    ap.add_argument("--iface", required=True, help="NIC name, e.g. enp1s0f1")
    ap.add_argument("--group", required=True, help="Multicast group, e.g. 239.100.3.1")
    ap.add_argument("--port", type=int, default=5000, help="UDP dest port (default 5000)")
    ap.add_argument("--local-port", type=int, required=True, help="Forward to 127.0.0.1:LOCAL")
    ap.add_argument("--local-host", default="127.0.0.1", help="Forward host (default 127.0.0.1)")
    ap.add_argument("--stats-every", type=float, default=5.0, help="Print stats interval seconds")
    args = ap.parse_args()

    # Outgoing localhost socket
    out = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    # Packet socket on interface
    raw = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(ETH_P_ALL))
    raw.bind((args.iface, 0))

    # Best-effort: enable promisc via packet mreq (linux)
    try:
        # PACKET_ADD_MEMBERSHIP / PACKET_MR_PROMISC = 1
        PACKET_ADD_MEMBERSHIP = 1
        PACKET_MR_PROMISC = 1
        # struct packet_mreq: mr_ifindex, mr_type, mr_alen, mr_address[8]
        ifindex = socket.if_nametoindex(args.iface)
        mreq = struct.pack("IHH8s", ifindex, PACKET_MR_PROMISC, 0, b"\x00" * 8)
        raw.setsockopt(socket.SOL_PACKET, PACKET_ADD_MEMBERSHIP, mreq)
        print("promisc: on", args.iface, flush=True)
    except Exception as e:
        print("promisc: skip (%s)" % e, flush=True)

    # Also try to IGMP-join (may fail on this host; relay still works in promisc)
    try:
        igmp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        igmp.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            igmp.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, args.iface.encode() + b"\0")
        except Exception:
            pass
        igmp.bind(("", args.port))
        ifindex = socket.if_nametoindex(args.iface)
        mreqn = struct.pack(
            "4s4si",
            socket.inet_aton(args.group),
            socket.inet_aton("0.0.0.0"),
            ifindex,
        )
        igmp.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreqn)
        print("igmp join: ok", args.group, "ifindex", ifindex, flush=True)
    except Exception as e:
        print("igmp join: skip (%s) — continuing with packet capture" % e, flush=True)
        igmp = None

    print(
        "relay %s %s:%d -> %s:%d"
        % (args.iface, args.group, args.port, args.local_host, args.local_port),
        flush=True,
    )
    print("monitor url hint: udp://%s:%d" % (args.local_host, args.local_port), flush=True)

    n_ok = n_bytes = 0
    t0 = time.time()
    last = t0
    dest = (args.local_host, args.local_port)

    try:
        while True:
            frame = raw.recv(65535)
            payload = parse_ipv4_udp_payload(frame, args.group, args.port)
            if not payload:
                continue
            out.sendto(payload, dest)
            n_ok += 1
            n_bytes += len(payload)
            now = time.time()
            if now - last >= args.stats_every:
                print(
                    "stats: pkts=%d bytes=%d rate=%.1f pkt/s"
                    % (n_ok, n_bytes, n_ok / max(now - t0, 1e-6)),
                    flush=True,
                )
                last = now
    except KeyboardInterrupt:
        print("stopped. total pkts=%d bytes=%d" % (n_ok, n_bytes), flush=True)
    finally:
        raw.close()
        out.close()
        if igmp is not None:
            igmp.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
