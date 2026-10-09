# -*- coding: utf-8 -*-
"""监测网口无载波时，该网卡上的节目中断，码率归零；重新收到包才恢复。"""
from __future__ import annotations

import socket
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "workers"))

from iface_mcast import (  # noqa: E402
    _deliver,
    apply_carrier_sample,
    iface_carrier_up,
    note_iface_link,
)
from monitor_worker import (  # noqa: E402
    LINK_DOWN_CONFIRM_SEC,
    capture_link_down,
    capture_status_rates,
    link_blocks_media,
)


def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


class LinkDownTests(unittest.TestCase):
    def test_carrier_file_wins(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(root / "eth0" / "carrier", "0\n")
            _write(root / "eth0" / "operstate", "up\n")
            _write(root / "eth1" / "carrier", "1\n")
            self.assertIs(iface_carrier_up("eth0", sysfs_root=str(root)), False)
            self.assertIs(iface_carrier_up("eth1", sysfs_root=str(root)), True)

    def test_operstate_when_carrier_unreadable(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(root / "eth0" / "operstate", "down\n")
            _write(root / "eth1" / "operstate", "lowerlayerdown\n")
            _write(root / "eth2" / "operstate", "up\n")
            _write(root / "eth3" / "operstate", "unknown\n")
            self.assertIs(iface_carrier_up("eth0", sysfs_root=str(root)), False)
            self.assertIs(iface_carrier_up("eth1", sysfs_root=str(root)), False)
            self.assertIs(iface_carrier_up("eth2", sysfs_root=str(root)), True)
            self.assertIsNone(iface_carrier_up("eth3", sysfs_root=str(root)))
            self.assertIsNone(iface_carrier_up("missing", sysfs_root=str(root)))
            self.assertIsNone(iface_carrier_up("../eth0", sysfs_root=str(root)))

    def test_down_zeros_rates_and_keeps_counters(self):
        hub = {
            "iface": "enp1s0f1",
            "group": "239.100.3.1",
            "mport": 5000,
            "stats": {
                "pkts": 280008,
                "bytes": 900000,
                "pkt_rate": 4644.7,
                "bitrate_kbps": 23874.6,
                "programs": {106: 6879.7},
                "skip": 0,
                "dests": 3,
                "ring_kb": 4096,
            },
        }
        self.assertEqual(apply_carrier_sample(hub, False, 1000.0), "down")
        st = hub["stats"]
        self.assertEqual(st["pkt_rate"], 0)
        self.assertEqual(st["bitrate_kbps"], 0)
        self.assertEqual(st["programs"], {})
        self.assertEqual(st["pkts"], 280008)
        self.assertEqual(st["bytes"], 900000)
        self.assertFalse(st["carrier"])
        self.assertTrue(st["link_was_down"])
        self.assertEqual(st["carrier_down_since"], 1000.0)
        self.assertIsNone(apply_carrier_sample(hub, False, 1004.0))
        self.assertEqual(hub["stats"]["carrier_down_since"], 1000.0)

    def test_up_keeps_zero_until_packets(self):
        hub = {"stats": {}}
        apply_carrier_sample(hub, False, 1000.0)
        self.assertEqual(apply_carrier_sample(hub, True, 1010.0), "up")
        st = hub["stats"]
        self.assertTrue(st["carrier"])
        self.assertTrue(st["link_was_down"])
        self.assertEqual(st["pkt_rate"], 0)
        self.assertEqual(st["bitrate_kbps"], 0)

    def test_unknown_and_quiet_link_do_not_zero(self):
        hub = {
            "stats": {
                "pkt_rate": 12.5,
                "bitrate_kbps": 1500.0,
                "programs": {1: 800.0},
                "carrier": True,
            }
        }
        before = dict(hub["stats"])
        self.assertIsNone(apply_carrier_sample(hub, None, 5.0))
        self.assertEqual(hub["stats"], before)
        self.assertIsNone(apply_carrier_sample(hub, True, 6.0))
        self.assertEqual(hub["stats"]["pkt_rate"], 12.5)
        self.assertEqual(hub["stats"]["programs"], {1: 800.0})
        self.assertNotIn("link_was_down", hub["stats"])

    def test_down_creates_stats_when_no_packet_yet(self):
        hub = {"iface": "enp1s0f1", "group": "239.1.1.1", "mport": 5000}
        self.assertEqual(apply_carrier_sample(hub, False, 50.0), "down")
        self.assertEqual(hub["stats"]["pkt_rate"], 0)
        self.assertFalse(hub["stats"]["carrier"])
        self.assertEqual(hub["stats"]["pkts"], 0)

    def test_note_logs_transition_once(self):
        class Log:
            def __init__(self):
                self.lines = []

            def warning(self, msg):
                self.lines.append(msg)

            def info(self, msg):
                self.lines.append(msg)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(root / "enp1s0f1" / "carrier", "0\n")
            hub = {"iface": "enp1s0f1", "group": "239.1.1.1", "mport": 5000}
            log = Log()
            state = {}
            note_iface_link(
                "enp1s0f1", [hub, hub], 10.0, logger=log, state=state, sysfs_root=str(root)
            )
            note_iface_link(
                "enp1s0f1", [hub], 11.0, logger=log, state=state, sysfs_root=str(root)
            )
            self.assertEqual(log.lines, ["iface capture enp1s0f1 link down"])
            self.assertEqual(hub["stats"]["carrier_down_since"], 10.0)
            _write(root / "enp1s0f1" / "carrier", "1\n")
            note_iface_link(
                "enp1s0f1", [hub], 20.0, logger=log, state=state, sysfs_root=str(root)
            )
            self.assertEqual(
                log.lines,
                [
                    "iface capture enp1s0f1 link down",
                    "iface capture enp1s0f1 link up",
                ],
            )
            self.assertTrue(hub["stats"]["link_was_down"])
            self.assertEqual(hub["stats"]["pkt_rate"], 0)

    def test_packet_window_clears_link_down(self):
        hub = {
            "iface": "enp1s0f1",
            "group": "239.1.1.1",
            "mport": 5000,
            "dest_lock": threading.Lock(),
            "ports": {},
            "feeders": {},
            "ring": None,
        }
        apply_carrier_sample(hub, False, 1000.0)
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            payload = b"\x47" + b"\x00" * 187
            _deliver(hub, payload, sock)
            hub["_win"]["t"] = time.time() - 1.2
            _deliver(hub, payload, sock)
        finally:
            sock.close()
        st = hub["stats"]
        self.assertTrue(st["carrier"])
        self.assertFalse(st["link_was_down"])
        self.assertGreater(st["pkt_rate"], 0)
        self.assertNotIn("carrier_down_since", st)

    def test_confirm_window_and_recovery_hold(self):
        since = 1000.0
        down = {
            "carrier": False,
            "link_was_down": True,
            "pkt_rate": 0,
            "carrier_down_since": since,
        }
        self.assertFalse(capture_link_down(down, since + LINK_DOWN_CONFIRM_SEC - 0.1))
        self.assertFalse(link_blocks_media(down, since + 1.0))
        self.assertTrue(link_blocks_media(down, since + LINK_DOWN_CONFIRM_SEC))
        held = {
            "carrier": True,
            "link_was_down": True,
            "pkt_rate": 0,
            "bitrate_kbps": 0,
        }
        self.assertFalse(capture_link_down(held, since + 30))
        self.assertTrue(link_blocks_media(held, since + 30))
        quiet = {"carrier": True, "pkt_rate": 0, "bitrate_kbps": 0}
        self.assertFalse(link_blocks_media(quiet, since + 30))
        self.assertFalse(link_blocks_media({}, since))
        self.assertFalse(link_blocks_media(None, since))
        back = {"carrier": True, "link_was_down": False, "pkt_rate": 100.0}
        self.assertFalse(link_blocks_media(back, since + 30))

    def test_status_rates_zero_while_down_or_held(self):
        stale = {
            "carrier": False,
            "pkt_rate": 4644.7,
            "bitrate_kbps": 23874.6,
            "program_bitrate_kbps": 6879.7,
        }
        self.assertEqual(
            capture_status_rates(stale),
            {"pkt_rate": 0, "bitrate_kbps": 0, "program_bitrate_kbps": 0},
        )
        held = {
            "carrier": True,
            "link_was_down": True,
            "pkt_rate": 0,
            "bitrate_kbps": 0,
            "program_bitrate_kbps": 6879.7,
        }
        self.assertEqual(capture_status_rates(held)["program_bitrate_kbps"], 0)
        live = {
            "carrier": True,
            "link_was_down": False,
            "pkt_rate": 80.0,
            "bitrate_kbps": 9000.0,
            "program_bitrate_kbps": 4000.0,
        }
        self.assertEqual(capture_status_rates(live)["program_bitrate_kbps"], 4000.0)
        mux = {"pkt_rate": 10.0, "bitrate_kbps": 500.0}
        self.assertNotIn("program_bitrate_kbps", capture_status_rates(mux))
        self.assertIsNone(capture_status_rates({}))
        self.assertIsNone(capture_status_rates(None))


if __name__ == "__main__":
    unittest.main()
