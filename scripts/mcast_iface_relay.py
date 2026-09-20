#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Multicast interface relay (CentOS 7 / Python 3.6).

AF_PACKET capture on NIC -> fan-out UDP payloads to one or more
127.0.0.1 local ports (so multiple FFmpeg/program monitors each get
a full copy; Linux unicast UDP cannot share one port safely).

Examples:
  python3 scripts/mcast_iface_relay.py --iface enp1s0f1 --group 239.100.3.1 \\
      --port 5000 --local-port 15501

  python3 scripts/mcast_iface_relay.py --iface enp1s0f1 --group 239.100.3.1 \\
      --port 5000 --local-ports 15501,15502,15503
"""

import argparse
import socket
import struct
import sys
import time

ETH_P_ALL = 0x0003
ETH_P_IP = 0x0800


def parse_ipv4_udp_payload(frame, group, udp_port):
    if len(frame) < 14:
        return None
    ethertype = struct.unpack("!H", frame[12:14])[0]
    off = 14
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
    if ip[9] != 17:
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
    ulen = struct.unpack("!H", udp[4:6])[0]
    payload = udp[8:ulen] if ulen >= 8 else udp[8:]
    return payload if payload else None


def main():
    ap = argparse.ArgumentParser(description="AF_PACKET multicast -> localhost UDP fan-out")
    ap.add_argument("--iface", required=True)
    ap.add_argument("--group", required=True)
    ap.add_argument("--port", type=int, default=5000)
    ap.add_argument("--local-port", type=int, default=None, help="single port (legacy)")
    ap.add_argument(
        "--local-ports",
        default=None,
        help="comma-separated ports, e.g. 15501,15502 (fan-out)",
    )
    ap.add_argument("--local-host", default="127.0.0.1")
    ap.add_argument("--stats-every", type=float, default=5.0)
    args = ap.parse_args()

    ports = []
    if args.local_ports:
        for p in str(args.local_ports).split(","):
            p = p.strip()
            if p:
                ports.append(int(p))
    if args.local_port is not None:
        ports.append(int(args.local_port))
    ports = sorted(set(ports))
    if not ports:
        print("need --local-port or --local-ports", file=sys.stderr)
        return 2

    out = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    raw = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(ETH_P_ALL))
    raw.bind((args.iface, 0))

    try:
        PACKET_ADD_MEMBERSHIP = 1
        PACKET_MR_PROMISC = 1
        ifindex = socket.if_nametoindex(args.iface)
        mreq = struct.pack("IHH8s", ifindex, PACKET_MR_PROMISC, 0, b"\x00" * 8)
        raw.setsockopt(socket.SOL_PACKET, PACKET_ADD_MEMBERSHIP, mreq)
        print("promisc: on", args.iface, flush=True)
    except Exception as e:
        print("promisc: skip (%s)" % e, flush=True)

    igmp = None
    try:
        igmp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        igmp.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            igmp.setsockopt(
                socket.SOL_SOCKET, socket.SO_BINDTODEVICE, args.iface.encode() + b"\0"
            )
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
        print("igmp join: skip (%s)" % e, flush=True)
        igmp = None

    dests = [(args.local_host, p) for p in ports]
    print(
        "relay %s %s:%d -> %s (fan-out %d)"
        % (args.iface, args.group, args.port, dests, len(dests)),
        flush=True,
    )

    n_ok = n_bytes = 0
    t0 = time.time()
    last = t0
    try:
        while True:
            frame = raw.recv(65535)
            payload = parse_ipv4_udp_payload(frame, args.group, args.port)
            if not payload:
                continue
            for dest in dests:
                try:
                    out.sendto(payload, dest)
                except Exception:
                    pass
            n_ok += 1
            n_bytes += len(payload)
            now = time.time()
            if now - last >= args.stats_every:
                print(
                    "stats: pkts=%d bytes=%d rate=%.1f pkt/s dests=%d"
                    % (n_ok, n_bytes, n_ok / max(now - t0, 1e-6), len(dests)),
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
