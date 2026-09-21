#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
In-process multicast capture hub (CentOS7 / Python 3.6).

One AF_PACKET reader per (iface, group, port). Each consumer gets one
localhost UDP port for the monitor FFmpeg.

A per-hub MPEG-TS ring buffer keeps recent packets so latest.jpg can be
extracted without a second UDP listener (which often hung / wrote nothing).
"""

from __future__ import print_function

import re
import socket
import struct
import threading
import time

_lock = threading.Lock()
_hubs = {}

ETH_P_ALL = 0x0003
ETH_P_IP = 0x0800

# ~4–8 Mbps HD ≈ 0.5–1 MB/s; keep enough for probe + a keyframe
_DEFAULT_RING_BYTES = 6 * 1024 * 1024


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
                # Prefer cutting on TS sync (0x47 every 188 bytes)
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


def _all_local_ports(hub):
    ports = []
    for item in hub["ports"].values():
        mon = item.get("mon")
        if mon:
            ports.append(mon)
        thumb = item.get("thumb")
        # legacy dual-port entries; skip if same as mon
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
            PACKET_ADD_MEMBERSHIP = 1
            PACKET_MR_PROMISC = 1
            ifindex = socket.if_nametoindex(iface)
            mreq = struct.pack("IHH8s", ifindex, PACKET_MR_PROMISC, 0, b"\x00" * 8)
            raw.setsockopt(socket.SOL_PACKET, PACKET_ADD_MEMBERSHIP, mreq)
        except Exception as e:
            if logger:
                logger.warning("promisc skip: %s" % e)
        if logger:
            logger.info(
                "iface capture thread on %s for %s:%s" % (iface, group, mport)
            )
        n = 0
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
                continue
            if ring is not None:
                try:
                    ring.write(payload)
                except Exception:
                    pass
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
                rsz = ring.size() if ring is not None else 0
                logger.info(
                    "iface capture %s:%s pkts=%d rate=%.1f dests=%d ring=%dKB"
                    % (
                        group,
                        mport,
                        n,
                        n / max(now - t0, 1e-6),
                        len(dests),
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

    thumb_url equals monitor_url: screenshots should use snapshot_ts() ring
    buffer, not a second UDP listener.
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
                "dest_lock": threading.Lock(),
                "stop": False,
                "thread": None,
                "logger": logger,
                "ring": _TsRing(_DEFAULT_RING_BYTES),
            }
            _hubs[key] = hub
        elif hub.get("ring") is None:
            hub["ring"] = _TsRing(_DEFAULT_RING_BYTES)

        def _listen_url(p):
            # FFmpeg 收流要用监听地址 udp://@:port（不要写 127.0.0.1）
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
                "iface capture %s mon=@:%d ring=on consumers=%d"
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
            empty = not hub["ports"]
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


def snapshot_ts(iface, group, port, min_bytes=300 * 1024):
    """
    Return a copy of the hub TS ring for (iface, group, port), or None.
    Used to feed ffmpeg stdin for latest.jpg without a second UDP bind.
    """
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
            logger.warning("iface=%s set but url is not udp multicast: %s" % (iface, url))
        return url, url, None
    mon_url, thumb_url = acquire(
        work_dir, iface, group, port, consumer_id=consumer_id, logger=logger
    )
    return mon_url, thumb_url, (iface, group, port, consumer_id)
