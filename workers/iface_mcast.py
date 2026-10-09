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
import os
import re
import socket
import struct
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


class _TsRing(object):
    """Packet deque ring; snapshot joins bytes. Avoids O(n) bytearray cuts."""

    def __init__(self, maxlen=_DEFAULT_RING_BYTES):
        self.maxlen = int(maxlen)
        self._q = collections.deque()
        self._nbytes = 0
        self._lock = threading.Lock()
        self.packets = 0

    def write(self, data):
        if not data:
            return
        with self._lock:
            self._q.append(data)
            self._nbytes += len(data)
            self.packets += 1
            while self._nbytes > self.maxlen and self._q:
                old = self._q.popleft()
                self._nbytes -= len(old)

    def snapshot(self, min_bytes=0):
        with self._lock:
            if self._nbytes < int(min_bytes):
                return None
            return b"".join(self._q)

    def size(self):
        with self._lock:
            return self._nbytes


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


def _raise_rmem_max(nbytes=128 * 1024 * 1024):
    """SO_RCVBUF 被 rmem_max(~208KB) 卡住时，16MB 形同虚设。"""
    path = "/proc/sys/net/core/rmem_max"
    try:
        cur = int(open(path, "r").read().strip())
    except Exception:
        return
    if cur >= int(nbytes):
        return
    try:
        open(path, "w").write("%d\n" % int(nbytes))
    except Exception:
        pass


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


def _parse_pmt(section):
    """Return (version, program_number, pcr_pid, [es_pid, ...]) or None."""
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
    while i + 5 <= body_end:
        epid = ((section[i + 1] & 0x1F) << 8) | section[i + 2]
        esil = ((section[i + 3] & 0x0F) << 8) | section[i + 4]
        if epid and epid != 0x1FFF:
            es.append(epid)
        i += 5 + esil
    return version, program, pcr, es


class _ProgramMeter(object):
    """Bytes of each service: its PMT, PCR and elementary PIDs. Not the whole mux."""

    def __init__(self):
        self.pmt_of = {}
        self.pids = {}
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
        for pid in list(self.pmt_ver):
            if pid not in progs.values():
                del self.pmt_ver[pid]
        self._reindex()

    def _take_pmt(self, section, pid):
        parsed = _parse_pmt(section)
        if not parsed:
            return
        version, program, pcr, es = parsed
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

    def roll(self, dt):
        dt = dt if dt and dt > 0 else 1e-6
        rates = {}
        for prog in self.pids:
            nbytes = self._win.get(prog, 0)
            rates[prog] = round(nbytes * 8.0 / dt / 1000.0, 1)
        self.rates = rates
        self._win = {}
        return rates


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


def _deliver(hub, payload, out_sock):
    ring = hub.get("ring")
    if ring is not None:
        try:
            ring.write(payload)
        except Exception:
            pass
    with hub["dest_lock"]:
        dests = list(_all_local_ports(hub))
        feeders = list((hub.get("feeders") or {}).values())
    for lp in dests:
        try:
            out_sock.sendto(payload, ("127.0.0.1", lp))
        except Exception:
            pass
    for feeder in feeders:
        try:
            feeder.put(payload)
        except Exception:
            pass
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
    try:
        meter.feed(payload)
    except Exception:
        pass
    now = time.time()
    if now - st["t"] >= 1.0:
        dt = max(now - st["t"], 1e-6)
        rsz = 0
        try:
            rsz = hub.get("ring").size() if hub.get("ring") else 0
        except Exception:
            pass
        try:
            programs = meter.roll(dt)
        except Exception:
            programs = {}
        with hub["dest_lock"]:
            nd = len(list(_all_local_ports(hub)))
        hub["stats"] = {
            "iface": hub.get("iface"),
            "group": hub.get("group"),
            "mport": hub.get("mport"),
            "pkts": st["pkts"],
            "skip": 0,
            "bytes": st["bytes"],
            "pkt_rate": round(st["n"] / dt, 1),
            "bitrate_kbps": round(st["b"] * 8.0 / dt / 1000.0, 1),
            "programs": programs,
            "dests": nd,
            "ring_kb": int(rsz / 1024),
            "updated_ts": now,
            "carrier": True,
            "link_was_down": False,
        }
        st["n"] = 0
        st["b"] = 0
        st["t"] = now
    return True


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
    out = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
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
        last_gen = cap.get("gen", 0)
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
                frame = raw.recv(2048)
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

            ip = _extract_ip(frame)
            if ip is None:
                n_skip += 1
                continue
            if reasm is not None:
                ip = reasm.feed(ip)
                if ip is None:
                    continue
            ihl = (ip[0] & 0x0F) * 4
            if ihl < 20 or len(ip) < ihl + 8:
                n_skip += 1
                continue
            dst = ip[16:20]
            dport = struct.unpack("!H", ip[ihl + 2 : ihl + 4])[0]
            hub = mapping.get((dst, dport))
            if hub is None:
                n_skip += 1
                continue
            payload = _udp_payload_from_ip(ip, hub["group"], hub["mport"])
            if not payload:
                n_skip += 1
                continue
            _deliver(hub, payload, out)
            n_match += 1
            last_match = time.time()
    except Exception as e:
        if logger:
            logger.error("iface capture thread error %s: %s" % (iface, e))
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
            logger.info("iface capture thread stopped %s" % iface)


def _ensure_capturer(iface, logger=None):
    with _lock:
        cap = _capturers.get(iface)
        if cap is not None and cap.get("thread") is not None and cap["thread"].is_alive():
            cap["gen"] = int(cap.get("gen") or 0) + 1
            if logger:
                cap["logger"] = logger
            return cap
        cap = {
            "stop": False,
            "thread": None,
            "gen": 1,
            "logger": logger,
        }
        _capturers[iface] = cap
        t = threading.Thread(
            target=_iface_loop,
            args=(iface, cap),
            name="mcap-%s" % iface,
            daemon=True,
        )
        cap["thread"] = t
        t.start()
    time.sleep(0.2)
    return cap


def _maybe_stop_capturer(iface, logger=None):
    with _lock:
        still = any(k[0] == iface for k in _hubs)
        if still:
            cap = _capturers.get(iface)
            if cap is not None:
                cap["gen"] = int(cap.get("gen") or 0) + 1
            return
        cap = _capturers.pop(iface, None)
    if cap is None:
        return
    cap["stop"] = True
    t = cap.get("thread")
    if t is not None and t.is_alive():
        t.join(timeout=3)
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
                "logger": logger,
                "ring": _TsRing(_DEFAULT_RING_BYTES),
                "prog": _ProgramMeter(),
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
            del _hubs[key]
            if logger:
                logger.info("stopped iface capture %s" % (key,))
        elif logger:
            logger.info(
                "iface capture release %s remaining=%d"
                % (consumer_id, len(hub["ports"]))
            )
    if empty:
        _maybe_stop_capturer(iface, logger)


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
