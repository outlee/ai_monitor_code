#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""静帧检测：墙钟 PTS、noise 方向、命令行不得再走 demux wallclock。"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "workers"))
sys.path.insert(0, str(ROOT))

from monitor_worker import StreamMonitor  # noqa: E402


def _mon(defaults=None, channel=None, work_dir=None):
    d = {
        "freeze_duration": 5.0,
        "freeze_noise": 0.02,
        "black_duration": 3.0,
        "detect_black": True,
        "detect_freeze": True,
        "detect_silence": False,
        "alarm_confirm_sec": 3.0,
        "freeze_confirm_sec": 2.0,
        "detect_width": 480,
        "log_dir": "logs",
        "snapshot_dir": "snapshots",
    }
    if defaults:
        d.update(defaults)
    ch = {"id": "t_freeze", "name": "t", "url": "udp://@239.1.1.1:5000", "enabled": True}
    if channel:
        ch.update(channel)
    wd = work_dir or tempfile.mkdtemp(prefix="amc-freeze-")
    return StreamMonitor(ch, d, wd)


class FreezeDetectConfigTests(unittest.TestCase):
    def test_noise_clamped_not_raised_to_0_08(self):
        m = _mon({"freeze_noise": 0.08})
        self.assertLessEqual(m.freeze_noise, 0.01)
        m2 = _mon({"freeze_noise": 0.003})
        self.assertAlmostEqual(m2.freeze_noise, 0.003)

    def test_duration_floor_12s(self):
        m = _mon({"freeze_duration": 5.0})
        self.assertEqual(m.freeze_duration, 12.0)

    def test_confirm_not_25s_band_aid(self):
        m = _mon({"freeze_confirm_sec": 2.0})
        self.assertGreaterEqual(m.freeze_confirm_sec, 3.0)
        self.assertLess(m.freeze_confirm_sec, 20.0)

    def test_filter_rewrites_pts_with_wallclock(self):
        m = _mon()
        fc = m._build_filter_complex()
        self.assertIn("setpts=(RTCTIME-RTCSTART)/1000000/TB", fc)
        self.assertIn("freezedetect=n=", fc)
        self.assertIn(":d=12.0", fc)

    def test_ffmpeg_cmd_drops_demux_wallclock_and_igndts(self):
        m = _mon()
        cmd = m._build_ffmpeg_cmd()
        joined = " ".join(cmd)
        self.assertNotIn("use_wallclock_as_timestamps", joined)
        self.assertNotIn("igndts", joined)
        self.assertIn("+genpts+discardcorrupt", joined)
        fc = cmd[cmd.index("-filter_complex") + 1]
        self.assertTrue(fc.startswith("[0:v]") or "setpts=" in fc)


if __name__ == "__main__":
    unittest.main()
