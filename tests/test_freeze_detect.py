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

from monitor_worker import (  # noqa: E402
    StreamMonitor,
    map_is_program,
    skip_other_program_map,
    thumb_tail_nbytes,
    thumb_video_maps,
)


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


class ThumbTailTests(unittest.TestCase):
    def test_fast_mux_gets_about_eight_seconds(self):
        # 33Mbps * 8s = 33MB，落在 12MB 和 40MB 之间
        n = thumb_tail_nbytes(33000)
        self.assertEqual(n, int(33000 * 1000 / 8 * 8))
        self.assertGreater(n, 12 * 1024 * 1024)
        self.assertLess(n, 40 * 1024 * 1024)

    def test_slow_or_unknown_stays_at_twelve_megabytes(self):
        floor = 12 * 1024 * 1024
        self.assertEqual(thumb_tail_nbytes(0), floor)
        self.assertEqual(thumb_tail_nbytes(8000), floor)

    def test_very_fast_mux_caps_at_forty_megabytes(self):
        self.assertEqual(thumb_tail_nbytes(80000), 40 * 1024 * 1024)


class ProgramMapTests(unittest.TestCase):
    def test_decoded_program_does_not_fall_through_to_another(self):
        self.assertTrue(map_is_program(["-map", "0:p:109:v:0"]))
        self.assertFalse(map_is_program(["-map", "0:v:0"]))
        self.assertFalse(map_is_program([]))
        self.assertFalse(skip_other_program_map(["-map", "0:p:109:v"], True))
        self.assertTrue(skip_other_program_map(["-map", "0:v:0"], True))
        self.assertFalse(skip_other_program_map(["-map", "0:v:1"], False))
        scoped = thumb_video_maps(110)
        self.assertTrue(all(map_is_program(m) for m in scoped))
        self.assertFalse(any(item == "0:v:0" for m in scoped for item in m))
        plain = thumb_video_maps(None)
        self.assertEqual(plain[0], ["-map", "0:v:0"])


class FreezeDetectConfigTests(unittest.TestCase):
    def test_noise_clamped_not_raised_to_0_08(self):
        m = _mon({"freeze_noise": 0.08})
        self.assertLessEqual(m.freeze_noise, 0.01)
        m2 = _mon({"freeze_noise": 0.003})
        self.assertAlmostEqual(m2.freeze_noise, 0.003)

    def test_duration_is_typed_seconds(self):
        m = _mon({"freeze_duration": 5.0, "alarm_confirm_sec": 3.0})
        self.assertEqual(m.freeze_duration, 5.0)
        self.assertEqual(m.freeze_confirm_sec, 5.0)
        self.assertAlmostEqual(m.alarm_confirm_sec, 3.0)
        self.assertAlmostEqual(m._alarm_need_sec("freeze"), 5.0)
        self.assertAlmostEqual(m._freeze_detect_d, 1.0)

    def test_confirm_is_wall_clock_freeze_duration(self):
        m = _mon({"freeze_confirm_sec": 2.0, "freeze_duration": 5.0, "alarm_confirm_sec": 3.0})
        self.assertEqual(m.freeze_confirm_sec, 5.0)
        self.assertAlmostEqual(m._alarm_need_sec("freeze"), 5.0)

    def test_filter_rewrites_pts_monotone_and_short_trigger(self):
        m = _mon()
        fc = m._build_filter_complex()
        self.assertIn("setpts=N/25/TB", fc)
        self.assertIn("freezedetect=n=", fc)
        self.assertIn("freezedetect=n=%s:d=1.0" % m.freeze_noise, fc)
        self.assertIn("blackdetect=d=1.0", fc)
        self.assertNotIn(":d=2.0", fc)
        self.assertNotIn(":d=3.0", fc)
        self.assertNotIn(":d=5.0", fc)
        self.assertNotIn(":d=12.0", fc)
        self.assertNotIn("silencedetect", fc)

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

    def test_video_silence_keeps_typed_seconds(self):
        m = _mon({"freeze_mode": "video_silence", "freeze_duration": 8})
        self.assertEqual(m.freeze_mode, "video_silence")
        self.assertEqual(m.freeze_duration, 8.0)
        self.assertEqual(m.freeze_confirm_sec, 8.0)
        fc = m._build_filter_complex()
        self.assertIn("silencedetect=", fc)
        self.assertIn(":d=1.0", fc)
        self.assertIn("Overall.Peak_level", fc)
        self.assertNotIn("silence_duration", fc)

    def test_level_meter_uses_real_audio_and_skips_null_source(self):
        m = _mon()
        fc = m._build_filter_complex()
        self.assertIn("astats=metadata=1", fc)
        self.assertIn("Overall.Peak_level", fc)
        m._audio_unavailable = True
        m.detect_silence = True
        m.freeze_mode = "video_silence"
        fc = m._build_filter_complex()
        self.assertIn("anullsrc=", fc)
        self.assertNotIn("astats=", fc)

    def test_peak_line_sets_level_without_raising_an_alarm(self):
        m = _mon()
        m._parse_ffmpeg_line(
            "[Parsed_ametadata_1] lavfi.astats.Overall.Peak_level=-18.063656"
        )
        self.assertEqual(m._audio_db, -18.1)
        self.assertEqual(m._audio_state(), "level")
        self.assertFalse(m._active_alarms)
        m._parse_ffmpeg_line(
            "[Parsed_ametadata_1] lavfi.astats.Overall.Peak_level=-inf"
        )
        self.assertEqual(m._audio_db, -120.0)
        self.assertEqual(m._audio_state(), "level")

    def test_video_silence_holds_while_audio_present(self):
        m = _mon({"freeze_mode": "video_silence", "freeze_duration": 8, "alarm_confirm_sec": 3})
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
        m._pending_alarms["freeze"] = {"event": ev, "since": time.time() - 9}
        m._flush_pending_alarms()
        self.assertNotIn("freeze", m._active_alarms)
        self.assertIn("freeze", m._pending_alarms)
        self.assertEqual(ev["message"], "检测到静帧")

    def test_video_silence_commits_when_audio_silent(self):
        m = _mon({"freeze_mode": "video_silence", "freeze_duration": 8, "alarm_confirm_sec": 3})
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
        m._silence_active = True
        m._silence_since = time.time() - 9
        m._pending_alarms["freeze"] = {"event": ev, "since": time.time() - 9}
        m._flush_pending_alarms()
        self.assertIn("freeze", m._active_alarms)
        self.assertEqual(ev["message"], "检测到静帧无伴音")

    def test_missing_audio_falls_back_to_picture(self):
        m = _mon({"freeze_mode": "video_silence", "freeze_duration": 8})
        m._audio_unavailable = True
        fc = m._build_filter_complex()
        self.assertIn("anullsrc=", fc)
        self.assertNotIn("silencedetect", fc)
        m._run_started_ts = time.time() - 60
        m.save_snapshot = False
        ev = {"type": "freeze", "message": "检测到静帧", "time": "t"}
        m._pending_alarms["freeze"] = {"event": ev, "since": time.time() - 9}
        m._flush_pending_alarms()
        self.assertIn("freeze", m._active_alarms)
        self.assertEqual(ev["message"], "检测到静帧")

    def test_startup_ignore_comes_from_config(self):
        m = _mon({"freeze_startup_ignore_sec": 7})
        self.assertEqual(m.freeze_startup_ignore_sec, 7.0)

    def test_black_need_is_black_duration(self):
        m = _mon({"black_duration": 3, "alarm_confirm_sec": 3})
        self.assertAlmostEqual(m._black_detect_d, 1.0)
        self.assertAlmostEqual(m._alarm_need_sec("black"), 3.0)
        self.assertAlmostEqual(m.alarm_confirm_sec, 3.0)

    def test_silence_uses_configured_duration_not_probe(self):
        m = _mon({
            "detect_silence": True,
            "silence_duration": 5.0,
            "freeze_mode": "video",
            "alarm_confirm_sec": 3.0,
        })
        self.assertTrue(m.detect_silence)
        self.assertAlmostEqual(m._silence_probe_d, 1.0)
        self.assertAlmostEqual(m._alarm_need_sec("silence"), 5.0)
        fc = m._build_filter_complex()
        self.assertIn("silencedetect=", fc)
        self.assertIn(":d=1.0", fc)
        self.assertNotIn(":d=5.0", fc)
        m._run_started_ts = time.time() - 60
        m.save_snapshot = False
        m._emit_alarm_event(
            alarm_key="silence",
            is_start=True,
            is_end=False,
            event={"type": "silence", "message": "检测到无伴音", "time": "t"},
        )
        item = m._pending_alarms["silence"]
        elapsed = time.time() - item["since"]
        self.assertGreater(elapsed, 0.5)
        self.assertLess(elapsed, 1.6)
        m._flush_pending_alarms()
        self.assertNotIn("silence", m._active_alarms)
        item["since"] = time.time() - 5
        m._flush_pending_alarms()
        self.assertIn("silence", m._active_alarms)
        self.assertEqual(item["event"]["message"], "检测到无伴音")

    def test_ai_does_not_open_udp_while_capture_is_on(self):
        m = _mon()
        m._capture_key = ("enp1s0f1", "239.1.1.1", 5000, 1)
        self.assertFalse(m._ai_may_grab_udp())

    def test_ai_confirm_waits_then_cools_down(self):
        m = _mon()
        m.save_snapshot = False

        class _FakeAI:
            is_ready = True
            confirm_sec = 6.0

            def analyze_image(self, path):
                return {
                    "is_anomaly": True,
                    "label": "mosaic",
                    "score": 0.9,
                    "backend": "builtin",
                    "detail": {},
                }

        m.ai = _FakeAI()
        saved = []
        m._save_event = lambda ev: saved.append(ev)
        frame = Path(m.work_dir) / "frame.jpg"
        frame.write_bytes(b"\xff\xd8" + b"x" * 3000 + b"\xff\xd9")
        m._analyze_frame_for_ai(frame)
        self.assertEqual(saved, [])
        self.assertIsNotNone(m._ai_bad_since)
        m._ai_bad_since = time.time() - 7
        m._analyze_frame_for_ai(frame)
        self.assertEqual(len(saved), 1)
        self.assertEqual(saved[0]["type"], "ai_mosaic")
        self.assertEqual(saved[0]["message"], "检测到马赛克")
        m._analyze_frame_for_ai(frame)
        self.assertEqual(len(saved), 1)

    def test_same_jpeg_does_not_confirm_alone(self):
        m = _mon()
        m.save_snapshot = False

        class _FakeAI:
            is_ready = True
            confirm_sec = 6.0

            def analyze_image(self, path):
                return {
                    "is_anomaly": True,
                    "label": "green_screen",
                    "score": 0.9,
                    "backend": "builtin",
                    "detail": {},
                }

        m.ai = _FakeAI()
        saved = []
        m._save_event = lambda ev: saved.append(ev)
        blob = b"\xff\xd8" + b"g" * 3000 + b"\xff\xd9"
        latest = m.snapshot_dir / "latest_ai.jpg"
        latest.write_bytes(blob)
        self.assertTrue(m._run_ai_once())
        self.assertEqual(saved, [])
        m._ai_bad_since = time.time() - 30
        self.assertTrue(m._run_ai_once())
        self.assertEqual(saved, [])
        time.sleep(0.02)
        latest.write_bytes(blob + b"\xff\xd9")
        self.assertTrue(m._run_ai_once())
        self.assertEqual(len(saved), 1)
        self.assertEqual(saved[0]["message"], "检测到花屏")
        self.assertEqual(saved[0]["type"], "ai_green_screen")

    def test_conceal_bar_is_not_sent_to_ai(self):
        m = _mon()
        blob = b"\xff\xd8" + b"g" * 3000 + b"\xff\xd9"
        m._stash_rejected_thumb(blob, "conceal")
        self.assertFalse((m.snapshot_dir / "latest_ai.jpg").is_file())
        m._stash_rejected_thumb(blob, "green")
        self.assertEqual((m.snapshot_dir / "latest_ai.jpg").read_bytes(), blob)

    def test_stashed_green_does_not_replace_dashboard_frame(self):
        m = _mon()
        good = b"\xff\xd8" + b"o" * 3000 + b"\xff\xd9"
        bad = b"\xff\xd8" + b"g" * 3000 + b"\xff\xd9"
        (m.snapshot_dir / "latest.jpg").write_bytes(good)
        m._stash_ai_frame(bad)
        self.assertEqual((m.snapshot_dir / "latest.jpg").read_bytes(), good)
        self.assertEqual((m.snapshot_dir / "latest_ai.jpg").read_bytes(), bad)
        names = [p.name for p in m._ai_frame_candidates()]
        self.assertIn("latest_ai.jpg", names)

    def test_ai_green_frame_is_not_dropped(self):
        m = _mon()
        m.save_snapshot = False
        blob = b"\xff\xd8" + b"g" * 3000 + b"\xff\xd9"
        latest = m.snapshot_dir / "latest.jpg"
        latest.parent.mkdir(parents=True, exist_ok=True)
        latest.write_bytes(blob)
        data = m._read_jpeg_file(latest, 45)
        self.assertEqual(data, blob)


if __name__ == "__main__":
    unittest.main()
