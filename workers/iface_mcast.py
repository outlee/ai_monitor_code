#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Per-process multicast capture hub (CentOS7 / Python 3.6).

One AF_PACKET capture per (iface, group, port). Each consumer (FFmpeg /
program channel) gets its OWN 127.0.0.1 local port; the relay fan-outs
every packet to all local ports. This avoids Linux unicast UDP "only one
socket receives" when two programs share one MPTS.
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
# key -> {proc, ports: {consumer_id: local_port}, logf, work_dir}
_hubs = {}


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


def _relay_script_path(work_dir):
    candidates = [
        os.path.join(work_dir, "scripts", "mcast_iface_relay.py"),
        os.path.join(os.path.dirname(__file__), "..", "scripts", "mcast_iface_relay.py"),
    ]
    for p in candidates:
        p = os.path.abspath(p)
        if os.path.isfile(p):
            return p
    return None


def _stop_proc(ent):
    proc = ent.get("proc")
    if proc is not None and proc.poll() is None:
        try:
            proc.terminate()
            proc.wait(5)
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
    ent["proc"] = None
    ent["logf"] = None


def _start_proc(ent, iface, group, mport, logger=None):
    script = _relay_script_path(ent["work_dir"])
    if not script:
        raise RuntimeError("mcast_iface_relay.py not found under scripts/")
    ports = sorted(set(ent["ports"].values()))
    if not ports:
        return
    cmd = [
        sys.executable,
        script,
        "--iface",
        iface,
        "--group",
        group,
        "--port",
        str(int(mport)),
        "--local-ports",
        ",".join(str(p) for p in ports),
        "--stats-every",
        "30",
    ]
    log_dir = os.path.join(ent["work_dir"], "logs")
    if not os.path.isdir(log_dir):
        os.makedirs(log_dir)
    log_path = os.path.join(
        log_dir,
        "iface_capture_%s_%s_%s.log" % (iface, group.replace(".", "_"), mport),
    )
    logf = open(log_path, "a", buffering=1)
    logf.write(
        "\n===== start %s ports=%s =====\n"
        % (time.strftime("%Y-%m-%d %H:%M:%S"), ports)
    )
    proc = subprocess.Popen(
        cmd,
        cwd=ent["work_dir"],
        stdout=logf,
        stderr=subprocess.STDOUT,
    )
    ent["proc"] = proc
    ent["logf"] = logf
    ent["cmd"] = cmd
    if logger:
        logger.info(
            "iface capture pid=%s %s %s:%s fan-out %s"
            % (proc.pid, iface, group, mport, ports)
        )
    time.sleep(0.4)
    if proc.poll() is not None:
        raise RuntimeError(
            "iface capture exited early code=%s (need root? see %s)"
            % (proc.poll(), log_path)
        )


def acquire(work_dir, iface, group, port, consumer_id, logger=None):
    """
    Register consumer_id for (iface,group,port); return dedicated local url.
    """
    key = (iface, group, int(port))
    with _lock:
        ent = _hubs.get(key)
        if ent is None:
            ent = {
                "work_dir": work_dir,
                "ports": {},
                "proc": None,
                "logf": None,
            }
            _hubs[key] = ent

        if consumer_id in ent["ports"]:
            lp = ent["ports"][consumer_id]
            if ent["proc"] is not None and ent["proc"].poll() is None:
                return "udp://127.0.0.1:%d" % lp
            # process died — restart below

        local_port = _free_udp_port()
        ent["ports"][consumer_id] = local_port
        # restart capture with full fan-out list
        _stop_proc(ent)
        _start_proc(ent, iface, group, port, logger=logger)
        if logger:
            logger.info(
                "iface capture assign %s -> 127.0.0.1:%d (consumers=%d)"
                % (consumer_id, local_port, len(ent["ports"]))
            )
        return "udp://127.0.0.1:%d" % local_port


def release(iface, group, port, consumer_id, logger=None):
    key = (iface, group, int(port))
    with _lock:
        ent = _hubs.get(key)
        if not ent:
            return
        if consumer_id in ent["ports"]:
            del ent["ports"][consumer_id]
        if ent["ports"]:
            # still have consumers — restart with remaining ports
            _stop_proc(ent)
            try:
                _start_proc(ent, iface, group, port, logger=logger)
            except Exception as e:
                if logger:
                    logger.error("restart iface capture failed: %s" % e)
            if logger:
                logger.info(
                    "iface capture release %s remaining=%d"
                    % (consumer_id, len(ent["ports"]))
                )
            return
        _stop_proc(ent)
        del _hubs[key]
        if logger:
            logger.info("stopped iface capture %s" % (key,))


def resolve_ffmpeg_url(work_dir, url, iface, consumer_id, logger=None):
    if not iface:
        return url, None
    group, port = parse_udp_group_port(url)
    if not group:
        if logger:
            logger.warning("iface=%s set but url is not udp multicast: %s" % (iface, url))
        return url, None
    local_url = acquire(
        work_dir, iface, group, port, consumer_id=consumer_id, logger=logger
    )
    return local_url, (iface, group, port, consumer_id)
