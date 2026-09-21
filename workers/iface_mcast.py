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


def _parse_payload(frame, group, udp_port):
    """
    Extract complete UDP payload for group:port.
    Drops IP fragments and truncated frames — partial datagrams corrupt TS
    (PES mismatch / missing SPS) and make one-shot JPEG extract fail.
    """
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
    if version != 4 or ihl < 20 or len(ip) < ihl + 8:
        return None
    if ip[9] != 17:  # UDP
        return None

    # Reject IP fragments (need full UDP datagram)
    frag_field = struct.unpack("!H", ip[6:8])[0]
    frag_offset = frag_field & 0x1FFF
    more_fragments = bool(frag_field & 0x2000)
    if frag_offset != 0 or more_fragments:
        return None

    total_len = struct.unpack("!H", ip[2:4])[0]
    if total_len < ihl + 8 or len(ip) < total_len:
        # Truncated capture — do not feed garbage into TS
        return None

    dst = socket.inet_ntoa(ip[16:20])
    if dst != group:
        return None

    udp = ip[ihl:total_len]
    if len(udp) < 8:
        return None
    dport = struct.unpack("!H", udp[2:4])[0]
    if dport != udp_port:
        return None
    ulen = struct.unpack("!H", udp[4:6])[0]
    if ulen < 8 or len(udp) < ulen:
        return None
    payload = udp[8:ulen]
    if not payload:
        return None
    # Prefer TS-looking payloads (typical IPTV = N*188 starting at 0x47)
    if payload[0] != 0x47 and len(payload) >= 188:
        # Still accept — some packers don't align; ring align helps later
        pass
    return payload


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
    ring = hub.get("ring")
    raw = None
    out = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        raw = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(ETH_P_ALL))
        raw.bind((iface, 0))
        try:
            PACKET_MR_PROMISC = 1
            ifindex = socket.if_nametoindex(iface)
            mreq = struct.pack("IHH8s", ifindex, PACKET_MR_PROMISC, 0, b"\x00" * 8)
            raw.setsockopt(socket.SOL_PACKET, 1, mreq)  # PACKET_ADD_MEMBERSHIP=1
        except Exception as e:
            if logger:
                logger.warning("promisc skip: %s" % e)
        if logger:
            logger.info(
                "iface capture thread on %s for %s:%s" % (iface, group, mport)
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
            payload = _parse_payload(frame, group, mport)
            if not payload:
                n_skip += 1
                continue
            if ring is not None:
                try:
                    ring.write(payload)
                except Exception:
                    pass
            with hub["dest_lock"]:
                dests = list(_all_local_ports(hub))
                feeders = list(hub.get("feeders", {}).values())
            for lp in dests:
                try:
                    out.sendto(payload, ("127.0.0.1", lp))
                except Exception:
                    pass
            for feeder in feeders:
                try:
                    feeder.put(payload)
                except Exception:
                    pass
            n += 1
            now = time.time()
            if logger and now - last >= 30:
                rsz = ring.size() if ring is not None else 0
                logger.info(
                    "iface capture %s:%s pkts=%d rate=%.1f dests=%d feeders=%d ring=%dKB"
                    % (
                        group,
                        mport,
                        n,
                        n / max(now - t0, 1e-6),
                        len(dests),
                        len(feeders),
                        int(rsz / 1024),
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
    thumb_url equals monitor_url; use register_feeder() for screenshots.
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
                "iface capture %s mon=@:%d feeders=%d consumers=%d"
                % (consumer_id, mon, len(hub.get("feeders", {})), len(hub["ports"]))
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
