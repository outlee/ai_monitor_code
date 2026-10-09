#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
In-process multicast capture hub (CentOS7 / Python 3.6).

One AF_PACKET reader per NIC in this process (not per group). Kernel BPF
keeps only subscribed multicast; userspace fans out to localhost UDP.

Also maintains:
  - a TS ring (debug / fallback snapshot)
  - per-consumer feeders that continuously receive the same TS bytes
"""

from __future__ import print_function

import collections
import json
import mmap
import os
import re
import socket
import struct
import subprocess
import sys
import threading
import time

_lock = threading.RLock()
_hubs = {}
_capturers = {}  # iface -> {stop, thread, gen, logger}

SO_DETACH_FILTER = 27

ETH_P_ALL = 0x0003
ETH_P_IP = 0x0800

# ~6s of ~5Mbps MPTS；8MB 只有约 1.5s，tsdemux 抽不出完整 GOP
_DEFAULT_RING_BYTES = 64 * 1024 * 1024
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


class _MmapCounter(object):
    """写指针放在映射最前面的 8 字节，父子进程读的是同一块。"""

    def __init__(self, mm):
        self._mm = mm

    def get(self):
        return struct.unpack_from("Q", self._mm, 0)[0]

    def set(self, value):
        struct.pack_into("Q", self._mm, 0, int(value))


class _TsRing(object):
    """一块预先分配的环形缓冲。收包只往里拷贝，不再为每个 UDP 包挂一个长期对象。"""

    def __init__(self, maxlen=_DEFAULT_RING_BYTES, buf=None, lock=None, counter=None):
        if buf is None:
            cap = int(maxlen)
            aligned = cap // 188 * 188
            self.maxlen = aligned if aligned >= 188 else cap
            if self.maxlen < 1:
                self.maxlen = 188
            self._buf = bytearray(self.maxlen)
        else:
            cap = len(buf)
            aligned = cap // 188 * 188
            self.maxlen = aligned if aligned >= 188 else cap
            if self.maxlen < 1:
                self.maxlen = max(cap, 1)
            self._buf = buf
        self._w = 0
        self._counter = counter
        if lock is None:
            self._lock = threading.Lock()
        else:
            self._lock = lock
        self.packets = 0
        if counter is not None:
            try:
                self._w = int(counter.get())
            except Exception:
                self._w = 0

    def _load_w(self):
        if self._counter is None:
            return self._w
        try:
            return int(self._counter.get())
        except Exception:
            return self._w

    def _store_w(self, value):
        value = int(value)
        self._w = value
        if self._counter is not None:
            self._counter.set(value)

    def _write_at(self, data):
        n = len(data)
        cap = self.maxlen
        w = self._load_w()
        if n >= cap:
            self._buf[:] = data[-cap:]
            w = cap
        else:
            pos = w % cap
            end = pos + n
            buf = self._buf
            if end <= cap:
                buf[pos:end] = data
            else:
                first = cap - pos
                buf[pos:] = data[:first]
                buf[: n - first] = data[first:]
            w += n
        self._store_w(w)
        self.packets += 1

    def write(self, data):
        if not data:
            return
        # 共享环只有一个写进程。先写字节再发布写指针，读侧不拿锁。
        if self._counter is not None:
            self._write_at(data)
            return
        with self._lock:
            self._write_at(data)

    def _compose(self, raw, w, min_bytes, torn):
        cap = self.maxlen
        avail = cap if w >= cap else w
        if avail < 0:
            return None
        if w <= cap:
            logical = raw[:avail]
        else:
            pos = w % cap
            logical = raw[pos:] + raw[:pos]
        if torn:
            if torn >= len(logical):
                return None
            logical = logical[torn:]
        if len(logical) < int(min_bytes):
            return None
        if not isinstance(logical, bytes):
            logical = bytes(logical)
        return logical

    def snapshot(self, min_bytes=0):
        if self._counter is None:
            with self._lock:
                cap = self.maxlen
                w = self._load_w()
                return self._compose(self._buf, w, min_bytes, 0)
        # 拷贝期间写进程仍往最旧的位置覆盖。丢掉这段，留下的尾部是完整的。
        cap = self.maxlen
        w1 = self._load_w()
        raw = bytes(self._buf[:cap])
        w2 = self._load_w()
        if w2 < w1 or (w2 - w1) >= cap:
            return None
        if w1 <= cap:
            torn = w2 - cap if w2 > cap else 0
        else:
            torn = w2 - w1
        torn = (int(torn) + 187) // 188 * 188
        return self._compose(raw, w1, min_bytes, torn)

    def size(self):
        if self._counter is None:
            with self._lock:
                cap = self.maxlen
                w = self._load_w()
                return cap if w >= cap else w
        cap = self.maxlen
        w = self._load_w()
        return cap if w >= cap else w


class _PayloadQueue(object):
    """收包线程只把整包放进来。转发慢时丢掉最旧的转发副本，环形缓冲里已经有了。"""

    def __init__(self, max_bytes):
        self.max_bytes = int(max_bytes)
        self._q = collections.deque()
        self._nbytes = 0
        self._cv = threading.Condition()
        self.dropped = 0

    def put(self, hub, payload):
        size = len(payload)
        with self._cv:
            self._q.append((size, hub, payload))
            self._nbytes += size
            while self._nbytes > self.max_bytes and len(self._q) > 1:
                old_size, _hub, _payload = self._q.popleft()
                self._nbytes -= old_size
                self.dropped += 1
            self._cv.notify()

    def get(self, timeout=0.5):
        with self._cv:
            if not self._q:
                self._cv.wait(timeout)
            if not self._q:
                return None
            size, hub, payload = self._q.popleft()
            self._nbytes -= size
            return hub, payload


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


def _bpf_dst_ips(ip_ints):
    """Accept IPv4 UDP (or fragments) to any of dest IPs. Untagged + VLAN."""
    ips = []
    seen = set()
    for x in ip_ints:
        x = int(x) & 0xFFFFFFFF
        if x not in seen:
            seen.add(x)
            ips.append(x)
    if not ips:
        return _assemble_bpf([("ret", 0)])

    def _ip_chain(prefix, chk):
        ops = []
        for i, ip in enumerate(ips):
            nxt = "%s_%d" % (prefix, i + 1) if i + 1 < len(ips) else "drop"
            ops.append(("jeq", ip, chk, nxt))
            if i + 1 < len(ips):
                ops.append(("label", "%s_%d" % (prefix, i + 1)))
        return ops

    ops = [
        ("ldh", 12),
        ("jeq", 0x8100, "vlan", "untag"),
        ("label", "untag"),
        ("ldh", 12),
        ("jeq", 0x0800, "ip4", "drop"),
        ("label", "ip4"),
        ("ld", 30),
    ]
    ops.extend(_ip_chain("n", "chk4"))
    ops.extend(
        [
            ("label", "chk4"),
            ("ldh", 20),
            ("jset", 0x1FFF, "accept", "p4"),
            ("label", "p4"),
            ("ldb", 23),
            ("jeq", 17, "accept", "drop"),
            ("label", "vlan"),
            ("ldh", 16),
            ("jeq", 0x0800, "ip4v", "drop"),
            ("label", "ip4v"),
            ("ld", 34),
        ]
    )
    ops.extend(_ip_chain("v", "chkv"))
    ops.extend(
        [
            ("label", "chkv"),
            ("ldh", 24),
            ("jset", 0x1FFF, "accept", "pv"),
            ("label", "pv"),
            ("ldb", 27),
            ("jeq", 17, "accept", "drop"),
            ("label", "accept"),
            ("ret", 65535),
            ("label", "drop"),
            ("ret", 0),
        ]
    )
    return _assemble_bpf(ops)


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


# 分片不能在快路径里当完整 UDP 用，交给重组。
_NEED_REASM = object()


def _udp_from_frame(frame):
    """从一个以太网帧里取出未分片的 IPv4/UDP 负载。

    返回 (目的地址 4 字节, 端口, 负载副本)。分片返回 _NEED_REASM。
    负载按 188 字节对齐，调用方可以立刻收下一帧。
    """
    n = len(frame)
    if n < 42:
        return None
    et = (frame[12] << 8) | frame[13]
    off = 14
    if et == 0x8100:
        if n < 46:
            return None
        et = (frame[16] << 8) | frame[17]
        off = 18
    if et != 0x0800:
        return None
    if n < off + 28:
        return None
    vihl = frame[off]
    if (vihl >> 4) != 4:
        return None
    ihl = (vihl & 0x0F) * 4
    if ihl < 20 or n < off + ihl + 8:
        return None
    frag = (frame[off + 6] << 8) | frame[off + 7]
    if frag & 0x3FFF:
        return _NEED_REASM
    if frame[off + 9] != 17:
        return None
    total = (frame[off + 2] << 8) | frame[off + 3]
    if total < ihl + 8:
        return None
    ip_end = off + total
    if ip_end > n:
        ip_end = n
    udp_off = off + ihl
    if udp_off + 8 > ip_end:
        return None
    dport = (frame[udp_off + 2] << 8) | frame[udp_off + 3]
    ulen = (frame[udp_off + 4] << 8) | frame[udp_off + 5]
    dst = bytes(frame[off + 16 : off + 20])
    pay_off = udp_off + 8
    pay_end = ip_end
    if ulen >= 8:
        want = udp_off + ulen
        if want <= pay_end:
            pay_end = want
    if pay_off >= pay_end:
        return None
    # 同步字节对齐时直接返回视图，避免每个 UDP 包再分配一份长期字节。
    if frame[pay_off] == 0x47:
        payload = frame[pay_off:pay_end]
        if len(payload) >= 188:
            payload = payload[: (len(payload) // 188) * 188]
        return (dst, dport, payload) if len(payload) else None
    payload = bytes(frame[pay_off:pay_end])
    if not payload:
        return None
    if payload[0] != 0x47:
        payload = _strip_rtp(payload)
        if payload and payload[0] != 0x47 and len(payload) >= 376:
            aligned = align_ts_sync(payload)
            if aligned and aligned[0] == 0x47:
                payload = aligned
    if not payload:
        return None
    if len(payload) >= 188:
        payload = payload[: (len(payload) // 188) * 188]
    return (dst, dport, payload) if payload else None


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


def _raise_core_max(path, nbytes):
    try:
        with open(path, "r") as fh:
            cur = int(fh.read().strip())
    except Exception:
        return
    if cur >= int(nbytes):
        return
    try:
        with open(path, "w") as fh:
            fh.write("%d\n" % int(nbytes))
    except Exception:
        pass


def _raise_rmem_max(nbytes=128 * 1024 * 1024):
    """SO_RCVBUF / SO_SNDBUF 被系统上限卡住时，套接字缓冲形同虚设。"""
    _raise_core_max("/proc/sys/net/core/rmem_max", nbytes)
    # 发送缓冲默认只有约 208KB。满了以后非阻塞 sendto 每个包都抛错，收包线程就慢下来。
    _raise_core_max("/proc/sys/net/core/wmem_max", 16 * 1024 * 1024)


def _hub_map_for_iface(iface):
    """(dst_ip_bytes, udp_port) -> hub"""
    mapping = {}
    ips = []
    with _lock:
        items = list(_hubs.items())
    for (iff, group, port), hub in items:
        if iff != iface:
            continue
        try:
            dst = socket.inet_aton(group)
        except Exception:
            continue
        mapping[(dst, int(port))] = hub
        ips.append(_ip4_to_int(group))
    return mapping, ips


def _psi_sections(pkt):
    """PSI sections that fit entirely in this 188-byte packet."""
    if not pkt or len(pkt) < 188 or pkt[0] != 0x47:
        return
    adapt = (pkt[3] >> 4) & 0x03
    off = 4
    if adapt & 0x02:
        if off >= 188:
            return
        alen = pkt[off]
        off += 1 + alen
    if not (adapt & 0x01) or off >= 188:
        return
    if pkt[1] & 0x40:
        pointer = pkt[off]
        off += 1 + pointer
    while off + 3 <= 188:
        if pkt[off] == 0xFF:
            return
        seclen = ((pkt[off + 1] & 0x0F) << 8) | pkt[off + 2]
        total = 3 + seclen
        if seclen < 1 or off + total > 188:
            return
        yield pkt[off : off + total]
        off += total


def _parse_pat(section):
    """Return (version, {program_number: pmt_pid}) or None."""
    if not section or section[0] != 0x00 or len(section) < 12:
        return None
    if (section[1] & 0x80) == 0 or (section[5] & 0x01) == 0:
        return None
    seclen = ((section[1] & 0x0F) << 8) | section[2]
    if seclen < 9 or 3 + seclen > len(section):
        return None
    version = (section[5] >> 1) & 0x1F
    body_end = 3 + seclen - 4
    progs = {}
    i = 8
    while i + 4 <= body_end:
        pnum = (section[i] << 8) | section[i + 1]
        pid = ((section[i + 2] & 0x1F) << 8) | section[i + 3]
        i += 4
        if pnum == 0 or pid == 0 or pid == 0x1FFF:
            continue
        progs[pnum] = pid
    return version, progs


# 广播里常见的 stream_type。0x06 私有流要再看描述符，避免把字幕当成伴音。
_VIDEO_STREAM = {
    0x01: "MPEG-1",
    0x02: "MPEG-2",
    0x10: "MPEG-4",
    0x1B: "H.264",
    0x24: "H.265",
    0x42: "AVS",
    0xD2: "AVS2",
}
_AUDIO_STREAM = {
    0x03: "MP2",
    0x04: "MP2",
    0x0F: "AAC",
    0x11: "AAC",
    0x81: "AC3",
    0x87: "EAC3",
}


def _private_audio_label(blob):
    """PES 私有流里的伴音名。没有已知音频描述符就返回空。"""
    data = blob if isinstance(blob, (bytes, bytearray)) else b""
    found = ""
    i = 0
    while i + 2 <= len(data):
        tag = data[i]
        ln = data[i + 1]
        if i + 2 + ln > len(data):
            break
        body = data[i + 2 : i + 2 + ln]
        i += 2 + ln
        if tag == 0x6A:
            found = "AC3"
        elif tag == 0x7A:
            found = "EAC3"
        elif tag == 0x7C:
            found = "AAC"
        elif tag == 0x05 and len(body) >= 4:
            reg = bytes(body[:4])
            if reg == b"AC-3":
                found = "AC3"
            elif reg in (b"EAC3", b"EC-3"):
                found = "EAC3"
            elif reg == b"DRA1":
                found = "DRA"
    return found


def _classify_es(stream_type, desc):
    """返回 (video|audio|'', 显示名)。"""
    if stream_type in _VIDEO_STREAM:
        return "video", _VIDEO_STREAM[stream_type]
    if stream_type in _AUDIO_STREAM:
        return "audio", _AUDIO_STREAM[stream_type]
    if stream_type == 0x06:
        label = _private_audio_label(desc)
        if label:
            return "audio", label
    return "", ""


def _parse_pmt(section):
    """Return (version, program_number, pcr_pid, [es_pid, ...], codec) or None.

    codec is {"video": name, "audio": name}. Audio may join two tracks with /.
    """
    if not section or section[0] != 0x02 or len(section) < 16:
        return None
    if (section[1] & 0x80) == 0 or (section[5] & 0x01) == 0:
        return None
    seclen = ((section[1] & 0x0F) << 8) | section[2]
    if seclen < 13 or 3 + seclen > len(section):
        return None
    version = (section[5] >> 1) & 0x1F
    program = (section[3] << 8) | section[4]
    pcr = ((section[8] & 0x1F) << 8) | section[9]
    pil = ((section[10] & 0x0F) << 8) | section[11]
    body_end = 3 + seclen - 4
    i = 12 + pil
    if i > body_end:
        return None
    es = []
    video = ""
    audios = []
    while i + 5 <= body_end:
        stream_type = section[i]
        epid = ((section[i + 1] & 0x1F) << 8) | section[i + 2]
        esil = ((section[i + 3] & 0x0F) << 8) | section[i + 4]
        desc_end = i + 5 + esil
        desc = section[i + 5 : desc_end] if desc_end <= body_end else b""
        if epid and epid != 0x1FFF:
            es.append(epid)
            kind, label = _classify_es(stream_type, desc)
            if kind == "video" and label and not video:
                video = label
            elif kind == "audio" and label and label not in audios and len(audios) < 2:
                audios.append(label)
        i += 5 + esil
    return version, program, pcr, es, {"video": video, "audio": "/".join(audios)}


class _ProgramMeter(object):
    """Bytes of each service: its PMT, PCR and elementary PIDs. Not the whole mux."""

    def __init__(self):
        self.pmt_of = {}
        self.pids = {}
        self.codecs = {}
        self.pat_ver = None
        self.pmt_ver = {}
        self._by_pid = {}
        self._pmt_pids = set()
        self._win = {}
        self.rates = {}

    def _reindex(self):
        by = {}
        for prog, pids in self.pids.items():
            for pid in pids:
                by.setdefault(pid, []).append(prog)
        self._by_pid = {pid: tuple(progs) for pid, progs in by.items()}
        self._pmt_pids = set(self.pmt_of.values())

    def _take_pat(self, section):
        parsed = _parse_pat(section)
        if not parsed:
            return
        version, progs = parsed
        if version == self.pat_ver and progs == self.pmt_of:
            return
        self.pat_ver = version
        self.pmt_of = progs
        for prog in list(self.pids):
            if prog not in progs:
                del self.pids[prog]
                self.codecs.pop(prog, None)
        for pid in list(self.pmt_ver):
            if pid not in progs.values():
                del self.pmt_ver[pid]
        self._reindex()

    def _take_pmt(self, section, pid):
        parsed = _parse_pmt(section)
        if not parsed:
            return
        version, program, pcr, es, codec = parsed
        if program <= 0:
            return
        known = self.pmt_of.get(program)
        if known is not None and known != pid:
            return
        if self.pmt_ver.get(pid) == version and program in self.pids:
            return
        pids = set(es)
        if pcr and pcr != 0x1FFF:
            pids.add(pcr)
        pids.add(pid)
        pids.discard(0)
        pids.discard(0x1FFF)
        self.pids[program] = pids
        self.codecs[program] = {
            "video": (codec or {}).get("video") or "",
            "audio": (codec or {}).get("audio") or "",
        }
        self.pmt_ver[pid] = version
        if known is None:
            self.pmt_of[program] = pid
        self._reindex()

    def feed(self, payload):
        if not payload:
            return
        n = len(payload) // 188
        data = payload
        win = self._win
        by = self._by_pid
        pmt_pids = self._pmt_pids
        for i in range(n):
            off = i * 188
            if data[off] != 0x47:
                continue
            pid = ((data[off + 1] & 0x1F) << 8) | data[off + 2]
            if pid == 0 or pid in pmt_pids:
                pkt = data[off : off + 188]
                if not isinstance(pkt, (bytes, bytearray)):
                    pkt = bytes(pkt)
                for sec in _psi_sections(pkt):
                    if pid == 0:
                        self._take_pat(sec)
                    else:
                        self._take_pmt(sec, pid)
                by = self._by_pid
                pmt_pids = self._pmt_pids
            if pid == 0 or pid == 0x1FFF:
                continue
            owners = by.get(pid)
            if not owners:
                continue
            for prog in owners:
                win[prog] = win.get(prog, 0) + 188

    def roll(self, dt, scale=1):
        dt = dt if dt and dt > 0 else 1e-6
        scale = scale if scale and scale > 0 else 1
        rates = {}
        for prog in self.pids:
            nbytes = self._win.get(prog, 0)
            rates[prog] = round(nbytes * scale * 8.0 / dt / 1000.0, 1)
        self.rates = rates
        self._win = {}
        return rates

    def codec_snapshot(self):
        out = {}
        for prog, info in (self.codecs or {}).items():
            info = info or {}
            out[prog] = {
                "video": info.get("video") or "",
                "audio": info.get("audio") or "",
            }
        return out


def pick_program_codec(codecs, program):
    """{"video", "audio"} for one service. None until that service is known.

    A channel with no program number uses the map only when the mux has one service.
    """
    if not codecs:
        return None
    if program is None:
        if len(codecs) != 1:
            return None
        return next(iter(codecs.values()))
    if program in codecs:
        return codecs[program]
    text = str(program)
    if text in codecs:
        return codecs[text]
    try:
        key = int(program)
    except (TypeError, ValueError):
        return None
    if key in codecs:
        return codecs[key]
    return None


def pick_program_bitrate(programs, program):
    """kbps for one service. None if this channel's program is not in the map yet.

    A channel with no program number uses the rate only when the mux has one service.
    """
    if not programs:
        return None
    if program is None:
        if len(programs) != 1:
            return None
        return next(iter(programs.values()))
    if program in programs:
        return programs[program]
    text = str(program)
    if text in programs:
        return programs[text]
    try:
        key = int(program)
    except (TypeError, ValueError):
        return None
    if key in programs:
        return programs[key]
    return None


def iface_carrier_up(iface, sysfs_root="/sys/class/net"):
    """True if the NIC has carrier, False if it does not, None if unknown.

    Unknown (missing iface, unreadable sysfs) must not raise a link-down alarm.
    """
    name = str(iface or "").strip()
    if not name or "/" in name or "\\" in name or name in (".", ".."):
        return None
    base = os.path.join(sysfs_root, name)
    try:
        with open(os.path.join(base, "carrier"), "r") as fh:
            text = fh.read().strip()
        if text == "1":
            return True
        if text == "0":
            return False
    except OSError:
        pass
    try:
        with open(os.path.join(base, "operstate"), "r") as fh:
            state = fh.read().strip().lower()
    except OSError:
        return None
    if state in ("down", "lowerlayerdown", "notpresent"):
        return False
    if state == "up":
        return True
    return None


def apply_carrier_sample(hub, up, now):
    """Fold one carrier reading into hub['stats'].

    up True keeps the last rate. A real packet window is what clears
    link_was_down. up False zeros live rates immediately and remembers the
    first down timestamp. up None leaves the hub alone.
    Returns 'down', 'up', or None (no transition).
    """
    if up is None or hub is None:
        return None
    lock = hub.get("stat_lock")
    if lock is not None:
        lock.acquire()
    try:
        return _apply_carrier_sample(hub, up, now)
    finally:
        if lock is not None:
            lock.release()


def _apply_carrier_sample(hub, up, now):
    st = hub.get("stats")
    if up:
        if not isinstance(st, dict):
            return None
        prev = st.get("carrier")
        st = dict(st)
        st["carrier"] = True
        st["updated_ts"] = now
        hub["stats"] = st
        if prev is False:
            return "up"
        return None
    if not isinstance(st, dict):
        prev = None
        st = {
            "iface": hub.get("iface"),
            "group": hub.get("group"),
            "mport": hub.get("mport"),
            "pkts": 0,
            "skip": 0,
            "bytes": 0,
            "dests": 0,
            "ring_kb": 0,
        }
    else:
        prev = st.get("carrier")
        st = dict(st)
    if prev is not False or not st.get("carrier_down_since"):
        st["carrier_down_since"] = now
    st["carrier"] = False
    st["link_was_down"] = True
    st["pkt_rate"] = 0
    st["bitrate_kbps"] = 0
    st["programs"] = {}
    st["updated_ts"] = now
    hub["stats"] = st
    if prev is False:
        return None
    return "down"


def note_iface_link(iface, hubs, now, logger=None, state=None, sysfs_root="/sys/class/net"):
    """Sample this NIC once and apply it to every hub. Log down/up once."""
    up = iface_carrier_up(iface, sysfs_root=sysfs_root)
    if up is None:
        return None
    seen = {}
    for hub in hubs or ():
        if hub is None:
            continue
        key = id(hub)
        if key in seen:
            continue
        seen[key] = True
        apply_carrier_sample(hub, up, now)
    if state is not None:
        prev = state.get("carrier_up")
        if prev is None:
            state["carrier_up"] = bool(up)
            if up is False and logger:
                logger.warning("iface capture %s link down" % iface)
        elif bool(prev) != bool(up):
            state["carrier_up"] = bool(up)
            if logger:
                if up:
                    logger.info("iface capture %s link up" % iface)
                else:
                    logger.warning("iface capture %s link down" % iface)
    return up


def _remember_payload(hub, payload):
    """先写入截图环。这一步必须在转发之前完成，转发堵住时环里仍然连续。"""
    ring = hub.get("ring")
    if ring is None:
        return
    try:
        ring.write(payload)
    except Exception:
        pass


def _fanout_send(hub, payload, out_sock):
    """只把整包送给监测进程。码率在收包线程上计，避免和收包抢解释器。"""
    left = hub.get("_dest_left") or 0
    dests = hub.get("_dest_cache")
    if dests is None or left <= 0:
        with hub["dest_lock"]:
            dests = tuple(_all_local_ports(hub))
            feeders = tuple((hub.get("feeders") or {}).values())
        hub["_dest_cache"] = dests
        hub["_feeder_cache"] = feeders
        left = 128
    hub["_dest_left"] = left - 1
    feeders = hub.get("_feeder_cache") or ()
    if hub.get("_send_skip"):
        hub["_send_skip"] = int(hub["_send_skip"]) - 1
    else:
        for lp in dests:
            try:
                out_sock.sendto(payload, ("127.0.0.1", lp))
            except BlockingIOError:
                # 监测进程一时读不走。跳过后续几包，避免每个包都走异常。
                hub["_send_skip"] = 64
                break
            except Exception:
                pass
    for feeder in feeders:
        try:
            feeder.put(payload if isinstance(payload, (bytes, bytearray)) else bytes(payload))
        except Exception:
            pass
    return True


def _account_payload(hub, payload):
    """按收到的包滚动整路和分节目码率。转发丢掉的副本不从这里扣。"""
    st = hub.get("_win")
    if st is None:
        st = {"n": 0, "b": 0, "t": time.time(), "pkts": 0, "bytes": 0}
        hub["_win"] = st
    st["n"] += 1
    st["b"] += len(payload)
    st["pkts"] += 1
    st["bytes"] += len(payload)
    meter = hub.get("prog")
    if meter is None:
        meter = _ProgramMeter()
        hub["prog"] = meter
    # 分节目码率抽样。整路字节每包都计。抽样倍数在 roll 里乘回去。
    stride = int(hub.get("meter_stride") or 1)
    if stride < 1:
        stride = 1
    try:
        if stride == 1 or not meter.pids or (st["pkts"] % stride) == 0:
            meter.feed(payload)
    except Exception:
        pass
    now = time.time()
    if now - st["t"] < 1.0:
        return True
    dt = max(now - st["t"], 1e-6)
    rsz = 0
    try:
        rsz = hub.get("ring").size() if hub.get("ring") else 0
    except Exception:
        pass
    try:
        programs = meter.roll(dt, stride)
    except Exception:
        programs = {}
    try:
        codecs = meter.codec_snapshot()
    except Exception:
        codecs = {}
    with hub["dest_lock"]:
        nd = len(list(_all_local_ports(hub)))
    stats = {
        "iface": hub.get("iface"),
        "group": hub.get("group"),
        "mport": hub.get("mport"),
        "pkts": st["pkts"],
        "skip": 0,
        "bytes": st["bytes"],
        "pkt_rate": round(st["n"] / dt, 1),
        "bitrate_kbps": round(st["b"] * 8.0 / dt / 1000.0, 1),
        "programs": programs,
        "codecs": codecs,
        "dests": nd,
        "ring_kb": int(rsz / 1024),
        "updated_ts": now,
        "carrier": True,
        "link_was_down": False,
    }
    lock = hub.get("stat_lock")
    if lock is not None:
        lock.acquire()
    try:
        hub["stats"] = stats
    finally:
        if lock is not None:
            lock.release()
    st["n"] = 0
    st["b"] = 0
    st["t"] = now
    return True


def _fanout_payload(hub, payload, out_sock):
    _account_payload(hub, payload)
    return _fanout_send(hub, payload, out_sock)


def _deliver(hub, payload, out_sock):
    _remember_payload(hub, payload)
    return _fanout_payload(hub, payload, out_sock)


def _kernel_packet_stats(raw):
    """AF_PACKET 自上次读取后的到达数和丢包数。读取会把计数清零。"""
    try:
        blob = raw.getsockopt(getattr(socket, "SOL_PACKET", 263), 6, 8)
    except Exception:
        return 0, 0
    if not blob or len(blob) < 8:
        return 0, 0
    return struct.unpack("II", blob[:8])


def _apply_bpf(raw, ips, logger=None):
    if not ips:
        try:
            raw.setsockopt(socket.SOL_SOCKET, SO_DETACH_FILTER, 0)
        except Exception:
            pass
        return False
    _attach_bpf(raw, _bpf_dst_ips(ips), logger=logger)
    return True


def _iface_loop(iface, cap):
    logger = cap.get("logger")
    raw = None
    _raise_rmem_max()
    out = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        out.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 8 * 1024 * 1024)
    except Exception:
        pass
    # 发送缓冲满了就丢掉这一份监测副本，不能把收包线程堵在 sendto 上。
    out.setblocking(False)
    reasm = _IpReassembler()
    bpf_on = False
    try:
        _raise_rmem_max()
        raw = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(ETH_P_ALL))
        raw.bind((iface, 0))
        try:
            raw.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 32 * 1024 * 1024)
        except Exception:
            pass
        try:
            ifindex = socket.if_nametoindex(iface)
            mreq = struct.pack("IHH8s", ifindex, 1, 0, b"\x00" * 8)
            sol_packet = getattr(socket, "SOL_PACKET", 263)
            raw.setsockopt(sol_packet, 1, mreq)
        except Exception as e:
            if logger:
                logger.warning("packet promisc skip: %s" % e)
        try:
            import subprocess as _sp

            _sp.call(
                ["ip", "link", "set", "dev", iface, "promisc", "on"],
                stdout=_sp.DEVNULL,
                stderr=_sp.DEVNULL,
            )
        except Exception as e:
            if logger:
                logger.warning("ip promisc skip: %s" % e)
        raw.settimeout(1.0)
        mapping, ips = _hub_map_for_iface(iface)
        # 配置可能刚好在这次读取之后才到。第一轮强制再装一次，避免空表被当成最新。
        last_gen = int(cap.get("gen", 0) or 0) - 1
        try:
            bpf_on = _apply_bpf(raw, ips, logger)
        except Exception as e:
            bpf_on = False
            if logger:
                logger.warning("bpf attach fail, userspace match: %s" % e)
        if logger:
            logger.info(
                "iface capture %s groups=%d bpf=%s"
                % (iface, len(ips), "on" if bpf_on else "off")
            )
        n_match = 0
        n_skip = 0
        t0 = time.time()
        last_log = t0
        last_match = t0
        bpf_since = t0
        frame_buf = bytearray(2048)
        frame_view = memoryview(frame_buf)

        while not cap["stop"]:
            gen = cap.get("gen", 0)
            if gen != last_gen:
                mapping, ips = _hub_map_for_iface(iface)
                last_gen = gen
                try:
                    bpf_on = _apply_bpf(raw, ips, logger)
                    bpf_since = time.time()
                    last_match = bpf_since
                    if logger:
                        logger.info(
                            "iface capture %s bpf reload groups=%d bpf=%s"
                            % (iface, len(ips), "on" if bpf_on else "off")
                        )
                except Exception as e:
                    bpf_on = False
                    if logger:
                        logger.warning("bpf reload fail: %s" % e)
            try:
                nread = raw.recv_into(frame_buf)
            except socket.timeout:
                now = time.time()
                try:
                    note_iface_link(
                        iface, mapping.values(), now, logger=logger, state=cap
                    )
                except Exception as e:
                    if logger:
                        logger.debug("iface carrier check %s: %s" % (iface, e))
                if bpf_on and n_match == 0 and now - bpf_since >= 8.0:
                    try:
                        raw.setsockopt(socket.SOL_SOCKET, SO_DETACH_FILTER, 0)
                    except Exception:
                        pass
                    bpf_on = False
                    if logger:
                        logger.warning(
                            "iface capture %s bpf=off fallback (0 match in 8s)"
                            % iface
                        )
                if logger and now - last_log >= 30:
                    logger.info(
                        "iface capture %s bpf=%s match=%d skip=%d groups=%d"
                        % (
                            iface,
                            "on" if bpf_on else "off",
                            n_match,
                            n_skip,
                            len(mapping),
                        )
                    )
                    last_log = now
                    for hub in mapping.values():
                        st = hub.get("stats") or {}
                        logger.info(
                            "iface capture %s:%s pkts=%d rate=%.1f dests=%d skip=%d ring=%dKB"
                            % (
                                hub.get("group"),
                                hub.get("mport"),
                                int(st.get("pkts") or 0),
                                float(st.get("pkt_rate") or 0),
                                int(st.get("dests") or 0),
                                n_skip,
                                int(st.get("ring_kb") or 0),
                            )
                        )
                continue
            except Exception:
                if cap["stop"]:
                    break
                time.sleep(0.05)
                continue

            if nread < 42:
                n_skip += 1
                continue
            got = _udp_from_frame(frame_view[:nread])
            hub = None
            payload = None
            if got is _NEED_REASM:
                frame = bytes(frame_view[:nread])
                ip = _extract_ip(frame)
                if ip is not None and reasm is not None:
                    ip = reasm.feed(ip)
                if ip is None:
                    continue
                ihl = (ip[0] & 0x0F) * 4
                if ihl < 20 or len(ip) < ihl + 8:
                    n_skip += 1
                    continue
                dst = bytes(ip[16:20])
                dport = struct.unpack("!H", ip[ihl + 2 : ihl + 4])[0]
                hub = mapping.get((dst, dport))
                if hub is not None:
                    payload = _udp_payload_from_ip(ip, hub["group"], hub["mport"])
            elif got is not None:
                dst, dport, payload = got
                hub = mapping.get((dst, dport))
            if hub is None or not payload:
                n_skip += 1
                continue
            # 先入环再记账。监测副本非阻塞送出，送不走也不回头丢环里的包。
            _remember_payload(hub, payload)
            try:
                _account_payload(hub, payload)
            except Exception:
                pass
            try:
                _fanout_send(hub, payload, out)
            except Exception:
                pass
            n_match += 1
            if (n_match & 4095) == 0:
                now = time.time()
                last_match = now
                if logger and now - last_log >= 30.0:
                    kpkts, kdrops = _kernel_packet_stats(raw)
                    logger.info(
                        "iface capture %s match=%d kernel_pkts=%d kernel_drops=%d skip=%d"
                        % (iface, n_match, kpkts, kdrops, n_skip)
                    )
                    last_log = now
    except Exception as e:
        if logger:
            logger.error("iface capture thread error %s: %s" % (iface, e))
    finally:
        cap["fan_stop"] = True
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
            logger.info("iface capture thread stopped %s" % iface)


def _ring_path(iface, group, port):
    safe_iface = re.sub(r"[^A-Za-z0-9]", "x", str(iface))
    safe_group = re.sub(r"[^A-Za-z0-9]", "d", str(group))
    return "/dev/shm/amcr_%d_%s_%s_%d" % (
        os.getpid(),
        safe_iface,
        safe_group,
        int(port),
    )


def _gc_ring_files():
    """清掉已经退出的监测进程留在 /dev/shm 里的环。"""
    try:
        names = os.listdir("/dev/shm")
    except Exception:
        return
    me = os.getpid()
    for name in names:
        if not name.startswith("amcr_"):
            continue
        parts = name.split("_")
        if len(parts) < 3:
            continue
        try:
            pid = int(parts[1])
        except ValueError:
            continue
        if pid == me:
            continue
        try:
            os.kill(pid, 0)
            alive = True
        except OSError:
            alive = False
        if alive:
            continue
        try:
            os.unlink("/dev/shm/" + name)
        except Exception:
            pass


def _open_shared_ring(path, create, nbytes=None):
    cap = int(nbytes or _DEFAULT_RING_BYTES)
    cap = cap // 188 * 188
    if cap < 188:
        cap = 188
    size = cap + 8
    flags = os.O_RDWR
    if create:
        flags |= os.O_CREAT
    fd = os.open(path, flags, 0o600)
    try:
        if create:
            os.ftruncate(fd, size)
        mm = mmap.mmap(fd, size)
        view = memoryview(mm)[8 : 8 + cap]
        ring = _TsRing(buf=view, counter=_MmapCounter(mm))
    except Exception:
        os.close(fd)
        raise
    ring._fd = fd
    ring._mm = mm
    ring._view = view
    ring._path = path
    return ring


def _close_ring(ring, unlink):
    if ring is None:
        return
    mm = getattr(ring, "_mm", None)
    fd = getattr(ring, "_fd", None)
    path = getattr(ring, "_path", None)
    try:
        if mm is not None:
            mm.close()
    except Exception:
        pass
    try:
        if fd is not None:
            os.close(fd)
    except Exception:
        pass
    if unlink and path:
        try:
            os.unlink(path)
        except OSError:
            pass


def _make_hub_ring(iface, group, port):
    try:
        _gc_ring_files()
        return _open_shared_ring(_ring_path(iface, group, port), True)
    except Exception:
        return _TsRing(_DEFAULT_RING_BYTES)


def _read_exact(fh, n):
    buf = bytearray()
    while len(buf) < n:
        try:
            chunk = fh.read(n - len(buf))
        except Exception:
            return None
        if not chunk:
            return None
        buf.extend(chunk)
    return bytes(buf)


def _write_msg(fh, obj, lock=None):
    data = json.dumps(obj, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
    blob = struct.pack("!I", len(data)) + data
    if lock is None:
        fh.write(blob)
        fh.flush()
        return
    with lock:
        fh.write(blob)
        fh.flush()


def _read_msg(fh):
    hdr = _read_exact(fh, 4)
    if not hdr:
        return None
    n = struct.unpack("!I", hdr)[0]
    if n <= 0 or n > 1024 * 1024:
        return None
    blob = _read_exact(fh, n)
    if blob is None:
        return None
    try:
        return json.loads(blob.decode("utf-8"))
    except Exception:
        return None


def _stats_for_json(st):
    programs = st.get("programs") or {}
    codecs = st.get("codecs") or {}
    out = {}
    for key, value in st.items():
        if key in ("programs", "codecs"):
            continue
        out[key] = value
    out["programs"] = dict((str(key), programs[key]) for key in programs)
    coded = {}
    for key, info in codecs.items():
        info = info or {}
        coded[str(key)] = {
            "video": info.get("video") or "",
            "audio": info.get("audio") or "",
        }
    out["codecs"] = coded
    return out


class _ProcLogger(object):
    def __init__(self, fh, lock):
        self._fh = fh
        self._lock = lock

    def _emit(self, level, msg):
        try:
            _write_msg(
                self._fh,
                {"op": "log", "level": level, "msg": str(msg)},
                self._lock,
            )
        except Exception:
            pass

    def info(self, msg):
        self._emit("info", msg)

    def warning(self, msg):
        self._emit("warning", msg)

    def error(self, msg):
        self._emit("error", msg)

    def debug(self, msg):
        self._emit("debug", msg)


def _apply_capture_config(iface, items):
    want = {}
    for item in items or ():
        try:
            key = (iface, item.get("group"), int(item.get("mport") or 0))
        except (TypeError, ValueError, AttributeError):
            continue
        want[key] = item
    with _lock:
        for key in list(_hubs):
            if key[0] != iface or key in want:
                continue
            hub = _hubs.pop(key, None)
            if hub is not None:
                _close_ring(hub.get("ring"), False)
        for key, item in want.items():
            hub = _hubs.get(key)
            if hub is None:
                try:
                    ring = _open_shared_ring(item.get("ring"), False)
                except Exception:
                    continue
                hub = {
                    "iface": iface,
                    "group": item.get("group"),
                    "mport": int(item.get("mport") or 0),
                    "ports": {},
                    "feeders": {},
                    "dest_lock": threading.Lock(),
                    "stat_lock": threading.Lock(),
                    "ring": ring,
                    "prog": _ProgramMeter(),
                    "meter_stride": int(item.get("meter_stride") or 8),
                }
                _hubs[key] = hub
            ports = {}
            for i, port in enumerate(item.get("ports") or []):
                try:
                    ports["p%d" % i] = {"mon": int(port)}
                except (TypeError, ValueError):
                    continue
            with hub["dest_lock"]:
                hub["ports"] = ports
            hub["_dest_cache"] = None
            hub["_dest_left"] = 0
            try:
                hub["meter_stride"] = int(item.get("meter_stride") or 8)
            except (TypeError, ValueError):
                hub["meter_stride"] = 8


def capture_process_main(iface):
    """单独进程收包。监测线程不再和收包抢同一个解释器。"""
    import sys

    try:
        os.nice(-5)
    except Exception:
        pass
    in_fh = sys.stdin.buffer
    out_fh = sys.stdout.buffer
    send_lock = threading.Lock()
    logger = _ProcLogger(out_fh, send_lock)
    cap = {"stop": False, "gen": 0, "logger": logger, "iface": iface}

    def reader():
        while not cap["stop"]:
            msg = _read_msg(in_fh)
            if not msg:
                cap["stop"] = True
                return
            op = msg.get("op")
            if op == "stop":
                cap["stop"] = True
                return
            if op != "config":
                continue
            try:
                _apply_capture_config(iface, msg.get("items") or [])
            except Exception as exc:
                logger.error("iface capture config %s: %s" % (iface, exc))
            cap["gen"] = int(cap.get("gen") or 0) + 1

    def publisher():
        while not cap["stop"]:
            time.sleep(0.5)
            batch = []
            with _lock:
                for key, hub in list(_hubs.items()):
                    if key[0] != iface:
                        continue
                    st = hub.get("stats")
                    if not st:
                        continue
                    batch.append((key[1], int(key[2]), _stats_for_json(st)))
            for group, mport, st in batch:
                if cap["stop"]:
                    return
                try:
                    _write_msg(
                        out_fh,
                        {
                            "op": "stats",
                            "group": group,
                            "mport": mport,
                            "stats": st,
                        },
                        send_lock,
                    )
                except Exception:
                    return

    threading.Thread(target=reader, name="mcap-cfg", daemon=True).start()
    threading.Thread(target=publisher, name="mcap-stat", daemon=True).start()
    _iface_loop(iface, cap)


def _capture_config_items(iface):
    with _lock:
        hubs = [hub for key, hub in _hubs.items() if key[0] == iface]
    items = []
    for hub in hubs:
        ring = hub.get("ring")
        path = getattr(ring, "_path", None)
        if not path:
            continue
        lock = hub.get("dest_lock")
        if lock is not None:
            lock.acquire()
        try:
            ports = [int(p) for p in _all_local_ports(hub)]
        finally:
            if lock is not None:
                lock.release()
        try:
            stride = int(hub.get("meter_stride") or 8)
        except (TypeError, ValueError):
            stride = 8
        items.append(
            {
                "group": hub.get("group"),
                "mport": int(hub.get("mport") or 0),
                "ring": path,
                "ports": ports,
                "meter_stride": stride,
            }
        )
    return items


def _rings_are_shared(iface):
    found = False
    for key, hub in _hubs.items():
        if key[0] != iface:
            continue
        found = True
        ring = hub.get("ring")
        if ring is None or not getattr(ring, "_path", None):
            return False
    return found


def _capture_alive(cap):
    proc = cap.get("proc")
    if proc is not None and proc.poll() is None:
        return True
    thread = cap.get("thread")
    return thread is not None and thread.is_alive()


def _close_proc_pipes(proc):
    if proc is None:
        return
    for fh in (proc.stdin, proc.stdout):
        try:
            if fh is not None:
                fh.close()
        except Exception:
            pass


def _start_capture_thread(cap):
    thread = cap.get("thread")
    if thread is not None and thread.is_alive():
        return
    iface = cap["iface"]
    t = threading.Thread(
        target=_iface_loop,
        args=(iface, cap),
        name="mcap-%s" % iface,
        daemon=True,
    )
    cap["thread"] = t
    cap["proc"] = None
    t.start()


def _spawn_capture_proc(cap):
    iface = cap["iface"]
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    code = (
        "import sys\n"
        "sys.path.insert(0, %r)\n"
        "from workers.iface_mcast import capture_process_main\n"
        "capture_process_main(%r)\n"
    ) % (root, iface)
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    return subprocess.Popen(
        [sys.executable, "-u", "-c", code],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=None,
        cwd=root,
        env=env,
        bufsize=0,
    )


def _apply_parent_msg(cap, msg):
    if not isinstance(msg, dict):
        return
    op = msg.get("op")
    logger = cap.get("logger")
    if op == "log":
        if logger is None:
            return
        level = str(msg.get("level") or "info")
        text = msg.get("msg") or ""
        fn = getattr(logger, level, None)
        if not callable(fn):
            fn = getattr(logger, "info", None)
        if not callable(fn):
            return
        try:
            fn(text)
        except Exception:
            pass
        return
    if op != "stats":
        return
    try:
        mport = int(msg.get("mport") or 0)
    except (TypeError, ValueError):
        return
    st = msg.get("stats")
    if not isinstance(st, dict):
        return
    key = (cap.get("iface"), msg.get("group"), mport)
    with _lock:
        hub = _hubs.get(key)
    if hub is None:
        return
    lock = hub.get("stat_lock")
    if lock is not None:
        lock.acquire()
    try:
        hub["stats"] = st
    finally:
        if lock is not None:
            lock.release()


def _send_capture_config(cap):
    proc = cap.get("proc")
    if proc is None or proc.poll() is not None:
        return False
    items = _capture_config_items(cap.get("iface"))
    try:
        _write_msg(
            proc.stdin,
            {"op": "config", "items": items},
            cap.get("send_lock"),
        )
        return True
    except Exception:
        return False


def _capture_reader(cap):
    fails = 0
    while not cap.get("stop"):
        proc = cap.get("proc")
        if proc is None:
            return
        while not cap.get("stop"):
            msg = _read_msg(proc.stdout)
            if msg is None:
                break
            fails = 0
            _apply_parent_msg(cap, msg)
        if cap.get("stop"):
            return
        code = proc.poll()
        logger = cap.get("logger")
        iface = cap.get("iface")
        if logger:
            try:
                logger.error("iface capture process exit %s code=%s" % (iface, code))
            except Exception:
                pass
        fails += 1
        _close_proc_pipes(proc)
        if fails >= 3:
            if logger:
                try:
                    logger.error("iface capture process fallback thread %s" % iface)
                except Exception:
                    pass
            _start_capture_thread(cap)
            return
        time.sleep(1.0)
        if cap.get("stop"):
            return
        try:
            cap["proc"] = _spawn_capture_proc(cap)
        except Exception as exc:
            if logger:
                try:
                    logger.error("iface capture process start %s: %s" % (iface, exc))
                except Exception:
                    pass
            _start_capture_thread(cap)
            return
        _send_capture_config(cap)


def _ensure_capturer(iface, logger=None):
    with _lock:
        cap = _capturers.get(iface)
        if cap is not None and _capture_alive(cap):
            cap["gen"] = int(cap.get("gen") or 0) + 1
            cap["iface"] = iface
            if logger:
                cap["logger"] = logger
            if cap.get("proc") is not None and cap["proc"].poll() is None:
                _send_capture_config(cap)
            return cap
        if cap is not None:
            cap["stop"] = True
            old = cap.get("proc")
            if old is not None and old.poll() is None:
                try:
                    old.kill()
                except Exception:
                    pass
        cap = {
            "stop": False,
            "thread": None,
            "proc": None,
            "gen": 1,
            "logger": logger,
            "iface": iface,
            "send_lock": threading.Lock(),
            "reader_on": False,
        }
        _capturers[iface] = cap
        use_process = _rings_are_shared(iface)
        spawned = None
        if use_process:
            try:
                spawned = _spawn_capture_proc(cap)
            except Exception as exc:
                use_process = False
                if logger:
                    logger.error("iface capture process start %s: %s" % (iface, exc))
        if not use_process or spawned is None:
            _start_capture_thread(cap)
        else:
            cap["proc"] = spawned
            cap["reader_on"] = True
            t = threading.Thread(
                target=_capture_reader,
                args=(cap,),
                name="mcap-rx-%s" % iface,
                daemon=True,
            )
            cap["reader"] = t
            t.start()
            _send_capture_config(cap)
            if logger:
                logger.info(
                    "iface capture process %s pid=%s" % (iface, spawned.pid)
                )
    if cap.get("thread") is not None:
        time.sleep(0.2)
    return cap


def _maybe_stop_capturer(iface, logger=None):
    with _lock:
        still = any(k[0] == iface for k in _hubs)
        cap = _capturers.get(iface)
        if still:
            if cap is not None:
                cap["gen"] = int(cap.get("gen") or 0) + 1
                if cap.get("proc") is not None and cap["proc"].poll() is None:
                    _send_capture_config(cap)
            return
        cap = _capturers.pop(iface, None)
    if cap is None:
        return
    cap["stop"] = True
    proc = cap.get("proc")
    if proc is not None and proc.poll() is None:
        try:
            _write_msg(proc.stdin, {"op": "stop"}, cap.get("send_lock"))
        except Exception:
            pass
        _close_proc_pipes(proc)
        try:
            proc.wait(timeout=3)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
    thread = cap.get("thread")
    if thread is not None and thread.is_alive():
        thread.join(timeout=3)
    if logger:
        logger.info("stopped iface capturer %s" % iface)


def acquire(work_dir, iface, group, port, consumer_id, logger=None):
    """
    Returns (monitor_url, thumb_url).

    只分配监测 UDP 口。截图从 TS ring 落盘后一次性抽帧。
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
                "stat_lock": threading.Lock(),
                "logger": logger,
                "ring": _make_hub_ring(iface, group, int(port)),
                "prog": _ProgramMeter(),
                # 8 抽 1 记分节目码率，把收包线程的时间留给连续入环。
                "meter_stride": 8,
            }
            _hubs[key] = hub
        else:
            if hub.get("ring") is None:
                hub["ring"] = _make_hub_ring(iface, group, int(port))
            if "feeders" not in hub:
                hub["feeders"] = {}
            if hub.get("stat_lock") is None:
                hub["stat_lock"] = threading.Lock()

        def _listen_url(p):
            return "udp://@:%d" % int(p)

        if consumer_id in hub["ports"]:
            item = hub["ports"][consumer_id]
            mon = item["mon"]
            return (_listen_url(mon), _listen_url(mon))

        mon = _free_udp_port()
        with hub["dest_lock"]:
            hub["ports"][consumer_id] = {"mon": mon}

        _ensure_capturer(iface, logger or hub.get("logger"))

        if logger:
            logger.info(
                "iface capture %s mon=@:%d ring=on consumers=%d"
                % (consumer_id, mon, len(hub["ports"]))
            )
        return (_listen_url(mon), _listen_url(mon))


def release(iface, group, port, consumer_id, logger=None):
    key = (iface, group, int(port))
    empty = False
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
            ring = hub.get("ring")
            del _hubs[key]
            _close_ring(ring, True)
            if logger:
                logger.info("stopped iface capture %s" % (key,))
        elif logger:
            logger.info(
                "iface capture release %s remaining=%d"
                % (consumer_id, len(hub["ports"]))
            )
    if empty:
        _maybe_stop_capturer(iface, logger)
        return
    with _lock:
        cap = _capturers.get(iface)
        if cap is not None and cap.get("proc") is not None and cap["proc"].poll() is None:
            _send_capture_config(cap)


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


def hub_stats(iface, group, port):
    """Return a copy of live capture stats for this multicast, or {}."""
    key = (iface, group, int(port))
    with _lock:
        hub = _hubs.get(key)
        if not hub:
            return {}
        st = hub.get("stats") or {}
        return dict(st)


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
