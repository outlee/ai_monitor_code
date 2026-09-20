#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Per-process multicast capture hub (CentOS7 / Python 3.6 friendly).

When FFmpeg cannot IP_ADD_MEMBERSHIP via localaddr, Worker can set channel
`iface: enp1s0f1` and keep the real multicast URL. This module starts ONE
AF_PACKET capture subprocess per (iface, group, port) and returns a local
UDP URL for FFmpeg. Multiple programs on the same MPTS share one capture.

Requires root (or CAP_NET_RAW) for AF_PACKET.
"""

from __future__ import print_function

import os
import re
import socket
import subprocess
import sys
import threading
import time
_lock = threading.Lock()
# key -> {"proc", "local_port", "refs", "local_url"}
_hubs = {}


def parse_udp_group_port(url):
    """
    Parse udp://@239.x.x.x:5000 or udp://239.x.x.x:5000?...
    Returns (group_ip, port) or (None, None).
    """
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


def _relay_script_path(work_dir):
    # prefer repo scripts/
    candidates = [
        os.path.join(work_dir, "scripts", "mcast_iface_relay.py"),
        os.path.join(os.path.dirname(__file__), "..", "scripts", "mcast_iface_relay.py"),
    ]
    for p in candidates:
        p = os.path.abspath(p)
        if os.path.isfile(p):
            return p
    return None


def acquire(work_dir, iface, group, port, logger=None):
    """
    Start or reuse capture for (iface, group, port).
    Returns local ffmpeg url udp://127.0.0.1:N
    """
    key = (iface, group, int(port))
    with _lock:
        ent = _hubs.get(key)
        if ent and ent["proc"].poll() is None:
            ent["refs"] += 1
            if logger:
                logger.info(
                    "reuse iface capture %s %s:%s -> %s (refs=%s)"
                    % (iface, group, port, ent["local_url"], ent["refs"])
                )
            return ent["local_url"]

        script = _relay_script_path(work_dir)
        if not script:
            raise RuntimeError("mcast_iface_relay.py not found under scripts/")

        local_port = _free_udp_port()
        cmd = [
            sys.executable,
            script,
            "--iface",
            iface,
            "--group",
            group,
            "--port",
            str(int(port)),
            "--local-port",
            str(local_port),
            "--stats-every",
            "30",
        ]
        logf = None
        try:
            log_dir = os.path.join(work_dir, "logs")
            if not os.path.isdir(log_dir):
                os.makedirs(log_dir)
            log_path = os.path.join(
                log_dir, "iface_capture_%s_%s_%s.log" % (iface, group.replace(".", "_"), port)
            )
            logf = open(log_path, "a", buffering=1)
            logf.write("\n===== start %s =====\n" % time.strftime("%Y-%m-%d %H:%M:%S"))
        except Exception:
            logf = subprocess.DEVNULL

        proc = subprocess.Popen(
            cmd,
            cwd=work_dir,
            stdout=logf if logf is not subprocess.DEVNULL else subprocess.DEVNULL,
            stderr=subprocess.STDOUT if logf is not subprocess.DEVNULL else subprocess.DEVNULL,
        )
        local_url = "udp://127.0.0.1:%d" % local_port
        _hubs[key] = {
            "proc": proc,
            "local_port": local_port,
            "local_url": local_url,
            "refs": 1,
            "logf": logf,
            "cmd": cmd,
        }
        if logger:
            logger.info(
                "start iface capture pid=%s %s %s:%s -> %s"
                % (proc.pid, iface, group, port, local_url)
            )
        # brief wait so first packets can flow
        time.sleep(0.5)
        if proc.poll() is not None:
            raise RuntimeError(
                "iface capture exited early code=%s (need root? see logs/iface_capture_*.log)"
                % proc.poll()
            )
        return local_url


def release(iface, group, port, logger=None):
    key = (iface, group, int(port))
    with _lock:
        ent = _hubs.get(key)
        if not ent:
            return
        ent["refs"] -= 1
        if ent["refs"] > 0:
            if logger:
                logger.info("iface capture refs now %s for %s" % (ent["refs"], key))
            return
        proc = ent["proc"]
        if proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        logf = ent.get("logf")
        if logf and logf is not subprocess.DEVNULL:
            try:
                logf.close()
            except Exception:
                pass
        del _hubs[key]
        if logger:
            logger.info("stopped iface capture %s" % (key,))


def resolve_ffmpeg_url(work_dir, url, iface, logger=None):
    """
    If iface set and url is multicast udp, return local relay url and a
    release callback info tuple; else return (url, None).
    """
    if not iface:
        return url, None
    group, port = parse_udp_group_port(url)
    if not group:
        if logger:
            logger.warning("iface=%s set but url is not udp multicast: %s" % (iface, url))
        return url, None
    local_url = acquire(work_dir, iface, group, port, logger=logger)
    return local_url, (iface, group, port)
