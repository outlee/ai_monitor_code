#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""静帧检测：墙钟 PTS、noise 方向、命令行不得再走 demux wallclock。"""
from __future__ import annotations

import sys
import tempfile
import time
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

    def test_confirm_is_wall_clock_freeze_duration(self):
        m = _mon({"freeze_confirm_sec": 2.0, "freeze_duration": 5.0})
        self.assertGreaterEqual(m.freeze_confirm_sec, 12.0)

    def test_filter_rewrites_pts_monotone_and_short_trigger(self):
        m = _mon()
        fc = m._build_filter_complex()
        self.assertIn("setpts=N/25/TB", fc)
        self.assertIn("freezedetect=n=", fc)
        self.assertIn(":d=2.0", fc)
        self.assertNotIn(":d=12.0", fc)

    def test_ffmpeg_cmd_drops_demux_wallclock_and_igndts(self):
        m = _mon()
        cmd = m._build_ffmpeg_cmd()
        joined = " ".join(cmd)
        self.assertNotIn("use_wallclock_as_timestamps", joined)
        self.assertNotIn("igndts", joined)
        self.assertIn("+genpts+discardcorrupt", joined)
        fc = cmd[cmd.index("-filter_complex") + 1]
        self.assertTrue(fc.startswith("[0:v]") or "setpts=" in fc)

    def test_freeze_end_before_wall_duration_does_not_commit(self):
        m = _mon()
        m._run_started_ts = time.time() - 60
        ev = {
            "type": "freeze",
            "phase": "start",
            "channel_id": m.id,
            "channel_name": m.name,
            "message": "检测到静帧",
            "time": "t",
        }
        m._emit_alarm_event(alarm_key="freeze", is_start=True, is_end=False, event=ev)
        self.assertIn("freeze", m._pending_alarms)
        m._emit_alarm_event(
            alarm_key="freeze",
            is_start=False,
            is_end=True,
            event={"type": "freeze_end", "duration": 3.2},
        )
        self.assertNotIn("freeze", m._pending_alarms)
        self.assertNotIn("freeze", m._active_alarms)

    def test_freeze_commits_only_after_wall_duration(self):
        m = _mon()
        m._run_started_ts = time.time() - 60
        m.save_snapshot = False
        ev = {
            "type": "freeze",
            "phase": "start",
            "channel_id": m.id,
            "channel_name": m.name,
            "message": "检测到静帧",
            "time": "t",
        }
        m._pending_alarms["freeze"] = {"event": ev, "since": time.time() - 3}
        m._flush_pending_alarms()
        self.assertNotIn("freeze", m._active_alarms)
        m._pending_alarms["freeze"] = {"event": ev, "since": time.time() - 13}
        m._flush_pending_alarms()
        self.assertIn("freeze", m._active_alarms)


if __name__ == "__main__":
    unittest.main()
