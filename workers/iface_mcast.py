#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
In-process multicast capture hub (CentOS7 / Python 3.6).

One AF_PACKET reader per (iface, group, port). Each consumer gets one
localhost UDP port for the monitor FFmpeg.

Also maintains:
  - a TS ring (debug / fallback snapshot)
  - per-consumer feeders that continuously receive the same TS bytes
    for a long-running thumb FFmpeg reading from stdin (keeps SPS/PPS)
"""

from __future__ import print_function

import collections
import re
import socket
import struct
import threading
import time

_lock = threading.Lock()
_hubs = {}

ETH_P_ALL = 0x0003
ETH_P_IP = 0x0800

# Debug / fallback snapshot size
_DEFAULT_RING_BYTES = 24 * 1024 * 1024
# Per-thumb stdin queue cap (drop oldest when full)
_DEFAULT_FEEDER_BYTES = 4 * 1024 * 1024


def align_ts_sync(data):
    """Find first MPEG-TS sync (0x47 every 188 bytes)."""
    if not data:
        return data
    n = len(data)
    limit = min(n - 188 * 2, 188 * 40)
    if limit < 0:
        return data
    for i in range(0, limit + 1):
        if (
            data[i] == 0x47
            and data[i + 188] == 0x47
            and data[i + 376] == 0x47
        ):
            if i == 0:
                return data
            return data[i:]
    return data


class _TsRing(object):
    """Thread-safe byte ring, trimmed on 188-byte TS boundaries when possible."""

    def __init__(self, maxlen=_DEFAULT_RING_BYTES):
        self.maxlen = int(maxlen)
        self._buf = bytearray()
        self._lock = threading.Lock()
        self.packets = 0
        self.bytes_in = 0

    def write(self, data):
        if not data:
            return
        with self._lock:
            self._buf.extend(data)
            self.packets += 1
            self.bytes_in += len(data)
            if len(self._buf) > self.maxlen:
                excess = len(self._buf) - self.maxlen
                cut = excess
                aligned = (excess // 188) * 188
                if aligned >= 188:
                    cut = aligned
                del self._buf[:cut]

    def snapshot(self, min_bytes=0):
        with self._lock:
            if len(self._buf) < int(min_bytes):
                return None
            return bytes(self._buf)

    def size(self):
        with self._lock:
            return len(self._buf)


class TsFeeder(object):
    """
    Bounded packet queue for one thumb FFmpeg stdin writer.
    When full, drops oldest packets so the decoder stays near live.
    """

    def __init__(self, maxlen=_DEFAULT_FEEDER_BYTES):
        self._maxlen = int(maxlen)
        self._cv = threading.Condition()
        self._q = collections.deque()
        self._nbytes = 0
        self._closed = False
        self.dropped = 0

    def put(self, data):
        if not data:
            return
        with self._cv:
            if self._closed:
                return
            self._q.append(data)
            self._nbytes += len(data)
            while self._nbytes > self._maxlen and self._q:
                old = self._q.popleft()
                self._nbytes -= len(old)
                self.dropped += 1
            self._cv.notify()

    def get_batch(self, max_bytes=256 * 1024, timeout=0.5):
        with self._cv:
            if not self._q and not self._closed:
                self._cv.wait(timeout)
            if not self._q:
                return b"" if self._closed else None
            parts = []
            size = 0
            while self._q and size < max_bytes:
                p = self._q.popleft()
                self._nbytes -= len(p)
                parts.append(p)
                size += len(p)
            return b"".join(parts)

    def close(self):
        with self._cv:
            self._closed = True
            self._cv.notify_all()

    def size(self):
        with self._cv:
            return self._nbytes


def parse_udp_group_port(url):
    if not url:
        return None, None
    u = url.strip()
    m = re.match(
        r"^udp://(?:[0-9.]+@)?@?([0-9.]+):(\d+)",
        u,
        re.IGNORECASE,
    )
    if not m:
        return None, None
    return m.group(1), int(m.group(2))


def _free_udp_port():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _ip4_to_int(ip):
    a, b, c, d = [int(x) for x in ip.split(".")]
    return (a << 24) | (b << 16) | (c << 8) | d


def _assemble_bpf(ops):
    labels = {}
    ins = []
    for op in ops:
        if op[0] == "label":
            labels[op[1]] = len(ins)
            continue
        ins.append(op)

    def rel(i, lab):
        if not lab:
            return 0
        return labels[lab] - (i + 1)

    out = []
    for i, op in enumerate(ins):
        n = op[0]
        if n == "ldh":
            out.append((0x28, 0, 0, op[1]))
        elif n == "ldb":
            out.append((0x30, 0, 0, op[1]))
        elif n == "ld":
            out.append((0x20, 0, 0, op[1]))
        elif n == "jeq":
            out.append((0x15, rel(i, op[2]), rel(i, op[3]), op[1] & 0xFFFFFFFF))
        elif n == "jset":
            out.append((0x45, rel(i, op[2]), rel(i, op[3]), op[1] & 0xFFFFFFFF))
        elif n == "ldxb_msh":
            out.append((0xB1, 0, 0, op[1]))
        elif n == "ldh_ind":
            out.append((0x48, 0, 0, op[1]))
        elif n == "ret":
            out.append((0x06, 0, 0, op[1] & 0xFFFFFFFF))
        else:
            raise ValueError("bad bpf op %r" % (op,))
    return out


def _bpf_udp_or_frag(group, port):
    """Accept IPv4 UDP to group:port, plus IP fragments to group (for reassembly)."""
    dst = _ip4_to_int(group)
    port = int(port)
    return _assemble_bpf(
        [
            ("ldh", 12),
            ("jeq", 0x8100, "vlan", "untag"),
            ("label", "untag"),
            ("ldh", 12),
            ("jeq", 0x0800, "ip4", "drop"),
            ("label", "ip4"),
            ("ld", 30),
            ("jeq", dst, "frag4", "drop"),
            ("label", "frag4"),
            ("ldh", 20),
            ("jset", 0x1FFF, "accept", "proto4"),
            ("label", "proto4"),
            ("ldb", 23),
            ("jeq", 17, "udp4", "drop"),
            ("label", "udp4"),
            ("ldxb_msh", 14),
            ("ldh_ind", 16),
            ("jeq", port, "accept", "drop"),
            ("label", "vlan"),
            ("ldh", 16),
            ("jeq", 0x0800, "ip4v", "drop"),
            ("label", "ip4v"),
            ("ld", 34),
            ("jeq", dst, "fragv", "drop"),
            ("label", "fragv"),
            ("ldh", 24),
            ("jset", 0x1FFF, "accept", "protov"),
            ("label", "protov"),
            ("ldb", 27),
            ("jeq", 17, "udpv", "drop"),
            ("label", "udpv"),
            ("ldxb_msh", 18),
            ("ldh_ind", 20),
            ("jeq", port, "accept", "drop"),
            ("label", "accept"),
            ("ret", 0x40000),
            ("label", "drop"),
            ("ret", 0),
        ]
    )


def _attach_bpf(sock, insns, logger=None):
    import ctypes

    SO_ATTACH_FILTER = 26
    raw = b"".join(
        struct.pack("HBBI", int(c), int(jt), int(jf), int(k) & 0xFFFFFFFF)
        for c, jt, jf, k in insns
    )
    buf = ctypes.create_string_buffer(raw)
    class SockFprog(ctypes.Structure):
        _fields_ = [("len", ctypes.c_ushort), ("filter", ctypes.c_void_p)]

    prog = SockFprog()
    prog.len = len(insns)
    prog.filter = ctypes.addressof(buf)
    libc = ctypes.CDLL("libc.so.6", use_errno=True)
    rc = libc.setsockopt(
        int(sock.fileno()),
        socket.SOL_SOCKET,
        SO_ATTACH_FILTER,
        ctypes.byref(prog),
        ctypes.sizeof(prog),
    )
    if rc != 0:
        err = ctypes.get_errno()
        raise OSError(err, "SO_ATTACH_FILTER")
    if logger:
        logger.info("iface BPF attached insns=%d" % len(insns))


class _IpReassembler(object):
    def __init__(self, timeout=1.5):
        self.timeout = timeout
        self._bufs = {}
        self._last_gc = time.time()

    def feed(self, ip):
        if not ip or len(ip) < 20:
            return None
        ihl = (ip[0] & 0x0F) * 4
        if ihl < 20 or len(ip) < ihl:
            return None
        total_len = struct.unpack("!H", ip[2:4])[0]
        if total_len < ihl or len(ip) < min(total_len, ihl):
            return None
        body_end = min(total_len, len(ip))
        ident = struct.unpack("!H", ip[4:6])[0]
        frag_field = struct.unpack("!H", ip[6:8])[0]
        mf = bool(frag_field & 0x2000)
        frag_off = (frag_field & 0x1FFF) * 8
        proto = ip[9]
        src = ip[12:16]
        dst = ip[16:20]
        key = (src, dst, ident, proto)
        payload = ip[ihl:body_end]
        if not mf and frag_off == 0:
            return ip[:body_end]
        now = time.time()
        rec = self._bufs.get(key)
        if rec is None:
            rec = {"parts": {}, "t": now, "end": None}
            self._bufs[key] = rec
        rec["parts"][frag_off] = payload
        rec["t"] = now
        if not mf:
            rec["end"] = frag_off + len(payload)
        if rec["end"] is None:
            self._gc(now)
            return None
        assembled = bytearray()
        off = 0
        while off < rec["end"]:
            part = rec["parts"].get(off)
            if part is None:
                self._gc(now)
                return None
            assembled.extend(part)
            off += len(part)
        if off != rec["end"]:
            return None
        del self._bufs[key]
        hdr = bytearray(ip[:ihl])
        total = ihl + len(assembled)
        struct.pack_into("!H", hdr, 2, total)
        struct.pack_into("!H", hdr, 6, 0)
        return bytes(hdr) + bytes(assembled)

    def _gc(self, now):
        if now - self._last_gc < 1.0:
            return
        self._last_gc = now
        dead = [k for k, v in self._bufs.items() if now - v["t"] > self.timeout]
        for k in dead:
            del self._bufs[k]


def _strip_rtp(payload):
    if not payload or len(payload) < 12 + 188:
        return payload
    if payload[0] == 0x47:
        return payload
    if (payload[0] >> 6) != 2:
        return payload
    cc = payload[0] & 0x0F
    off = 12 + 4 * cc
    if payload[1] & 0x10:  # extension
        if off + 4 > len(payload):
            return payload
        ext_len = struct.unpack("!H", payload[off + 2 : off + 4])[0]
        off += 4 + ext_len * 4
    if off < len(payload) and payload[off] == 0x47:
        return payload[off:]
    return payload


def _extract_ip(frame):
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
    if (ip[0] >> 4) != 4:
        return None
    return ip


def _udp_payload_from_ip(ip, group, udp_port):
    if not ip or len(ip) < 20:
        return None
    ihl = (ip[0] & 0x0F) * 4
    if ihl < 20 or len(ip) < ihl + 8:
        return None
    if ip[9] != 17:
        return None
    total_len = struct.unpack("!H", ip[2:4])[0]
    if total_len < ihl + 8:
        return None
    body_end = min(total_len, len(ip))
    dst = socket.inet_ntoa(ip[16:20])
    if dst != group:
        return None
    udp = ip[ihl:body_end]
    if len(udp) < 8:
        return None
    dport = struct.unpack("!H", udp[2:4])[0]
    if dport != udp_port:
        return None
    ulen = struct.unpack("!H", udp[4:6])[0]
    if ulen < 8 or len(udp) < ulen:
        # use whatever we have if length is truncated after reassembly fail
        payload = udp[8:]
    else:
        payload = udp[8:ulen]
    payload = _strip_rtp(payload)
    if not payload:
        return None
    if payload[0] != 0x47 and len(payload) >= 188 * 2:
        aligned = align_ts_sync(payload)
        if aligned and aligned[0] == 0x47:
            payload = aligned
    if len(payload) >= 188:
        n = (len(payload) // 188) * 188
        if n > 0:
            payload = payload[:n]
    return payload if payload else None


def _parse_payload(frame, group, udp_port, reasm=None):
    ip = _extract_ip(frame)
    if ip is None:
        return None
    if reasm is not None:
        ip = reasm.feed(ip)
        if ip is None:
            return None
    else:
        frag_field = struct.unpack("!H", ip[6:8])[0]
        if (frag_field & 0x1FFF) or (frag_field & 0x2000):
            return None
    return _udp_payload_from_ip(ip, group, udp_port)


def _all_local_ports(hub):
    ports = []
    for item in hub["ports"].values():
        mon = item.get("mon")
        if mon:
            ports.append(mon)
        thumb = item.get("thumb")
        if thumb and thumb != mon:
            ports.append(thumb)
    return ports


def _capture_loop(hub):
    iface = hub["iface"]
    group = hub["group"]
    mport = hub["mport"]
    logger = hub.get("logger")
    raw = None
    out = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    reasm = _IpReassembler()
    try:
        raw = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(ETH_P_ALL))
        raw.bind((iface, 0))
        try:
            raw.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 16 * 1024 * 1024)
        except Exception:
            pass
        try:
            ifindex = socket.if_nametoindex(iface)
            mreq = struct.pack("IHH8s", ifindex, 1, 0, b"\x00" * 8)  # PROMISC
            raw.setsockopt(socket.SOL_PACKET, 1, mreq)
        except Exception as e:
            if logger:
                logger.warning("promisc skip: %s" % e)
        # BPF 在部分网卡/VLAN 卸载场景会把组播全部滤掉 → 全频道断流。
        # 先不挂 BPF，仍靠用户态匹配 group:port（skip 会偏大，但能收到流）。
        if logger:
            logger.info(
                "iface capture thread on %s for %s:%s bpf=off"
                % (iface, group, mport)
            )
        n = 0
        n_skip = 0
        t0 = time.time()
        last = t0
        while not hub["stop"]:
            try:
                raw.settimeout(1.0)
                frame = raw.recv(65535)
            except socket.timeout:
                continue
            except Exception:
                if hub["stop"]:
                    break
                time.sleep(0.05)
                continue
            payload = _parse_payload(frame, group, mport, reasm=reasm)
            if not payload:
                n_skip += 1
                continue
            with hub["dest_lock"]:
                dests = list(_all_local_ports(hub))
            for lp in dests:
                try:
                    out.sendto(payload, ("127.0.0.1", lp))
                except Exception:
                    pass
            n += 1
            now = time.time()
            if logger and now - last >= 30:
                logger.info(
                    "iface capture %s:%s pkts=%d rate=%.1f dests=%d skip=%d"
                    % (
                        group,
                        mport,
                        n,
                        n / max(now - t0, 1e-6),
                        len(dests),
                        n_skip,
                    )
                )
                last = now
    except Exception as e:
        if logger:
            logger.error("iface capture thread error: %s" % e)
    finally:
        try:
            out.close()
        except Exception:
            pass
        if raw is not None:
            try:
                raw.close()
            except Exception:
                pass
        if logger:
            logger.info("iface capture thread stopped %s:%s" % (group, mport))


def acquire(work_dir, iface, group, port, consumer_id, logger=None):
    """
    Returns (monitor_url, thumb_url).

    只分配监测 UDP 口。截图由主 FFmpeg 旁路写 latest.jpg，不再为每路
    再开一个 thumb 口/解码器（dests 翻倍会把 AF_PACKET 拖垮）。
    """
    key = (iface, group, int(port))
    with _lock:
        hub = _hubs.get(key)
        if hub is None:
            hub = {
                "iface": iface,
                "group": group,
                "mport": int(port),
                "work_dir": work_dir,
                "ports": {},
                "feeders": {},
                "dest_lock": threading.Lock(),
                "stop": False,
                "thread": None,
                "logger": logger,
                "ring": _TsRing(_DEFAULT_RING_BYTES),
            }
            _hubs[key] = hub
        else:
            if hub.get("ring") is None:
                hub["ring"] = _TsRing(_DEFAULT_RING_BYTES)
            if "feeders" not in hub:
                hub["feeders"] = {}

        def _listen_url(p):
            return "udp://@:%d" % int(p)

        if consumer_id in hub["ports"]:
            item = hub["ports"][consumer_id]
            mon = item["mon"]
            return (_listen_url(mon), _listen_url(mon))

        mon = _free_udp_port()
        with hub["dest_lock"]:
            hub["ports"][consumer_id] = {"mon": mon}

        if hub["thread"] is None or not hub["thread"].is_alive():
            hub["stop"] = False
            hub["logger"] = logger or hub.get("logger")
            t = threading.Thread(
                target=_capture_loop,
                args=(hub,),
                name="mcap-%s-%s" % (iface, group),
                daemon=True,
            )
            hub["thread"] = t
            t.start()
            time.sleep(0.3)

        if logger:
            logger.info(
                "iface capture %s mon=@:%d consumers=%d"
                % (consumer_id, mon, len(hub["ports"]))
            )
        return (_listen_url(mon), _listen_url(mon))


def release(iface, group, port, consumer_id, logger=None):
    key = (iface, group, int(port))
    with _lock:
        hub = _hubs.get(key)
        if not hub:
            return
        with hub["dest_lock"]:
            if consumer_id in hub["ports"]:
                del hub["ports"][consumer_id]
            feeder = hub.get("feeders", {}).pop(consumer_id, None)
            empty = not hub["ports"]
        if feeder is not None:
            try:
                feeder.close()
            except Exception:
                pass
        if empty:
            hub["stop"] = True
            t = hub.get("thread")
            if t and t.is_alive():
                t.join(timeout=3)
            del _hubs[key]
            if logger:
                logger.info("stopped iface capture %s" % (key,))
        elif logger:
            logger.info(
                "iface capture release %s remaining=%d"
                % (consumer_id, len(hub["ports"]))
            )


def register_feeder(iface, group, port, consumer_id, feeder=None, logger=None):
    """Attach (or replace) a TsFeeder for continuous thumb stdin."""
    key = (iface, group, int(port))
    if feeder is None:
        feeder = TsFeeder()
    with _lock:
        hub = _hubs.get(key)
        if not hub:
            if logger:
                logger.warning("register_feeder: no hub for %s" % (key,))
            return None
        with hub["dest_lock"]:
            old = hub.setdefault("feeders", {}).get(consumer_id)
            hub["feeders"][consumer_id] = feeder
        if old is not None and old is not feeder:
            try:
                old.close()
            except Exception:
                pass
    return feeder


def unregister_feeder(iface, group, port, consumer_id):
    key = (iface, group, int(port))
    with _lock:
        hub = _hubs.get(key)
        if not hub:
            return
        with hub["dest_lock"]:
            feeder = hub.get("feeders", {}).pop(consumer_id, None)
    if feeder is not None:
        try:
            feeder.close()
        except Exception:
            pass


def snapshot_ts(iface, group, port, min_bytes=300 * 1024):
    key = (iface, group, int(port))
    with _lock:
        hub = _hubs.get(key)
        if not hub:
            return None
        ring = hub.get("ring")
        if ring is None:
            return None
    return ring.snapshot(min_bytes=min_bytes)


def ring_size(iface, group, port):
    key = (iface, group, int(port))
    with _lock:
        hub = _hubs.get(key)
        if not hub or hub.get("ring") is None:
            return 0
        return hub["ring"].size()


def resolve_ffmpeg_url(work_dir, url, iface, consumer_id, logger=None):
    """
    Returns (monitor_url, thumb_url, key).
    If no iface, returns (url, url, None).
    """
    if not iface:
        return url, url, None
    group, port = parse_udp_group_port(url)
    if not group:
        if logger:
            logger.warning(
                "iface=%s set but url is not udp multicast: %s" % (iface, url)
            )
        return url, url, None
    mon_url, thumb_url = acquire(
        work_dir, iface, group, port, consumer_id=consumer_id, logger=logger
    )
    return mon_url, thumb_url, (iface, group, port, consumer_id)
