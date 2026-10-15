# -*- coding: utf-8 -*-
"""异常截图按时间分页，实时图不进列表。"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "web"))


def _stub_web_deps():
    """本机解释器没有 fastapi 时仍能导入列表函数。"""
    import types

    if "event_db" not in sys.modules:
        event_db = types.ModuleType("event_db")
        event_db.configure = lambda *a, **k: None
        sys.modules["event_db"] = event_db
    if "fastapi" in sys.modules:
        return

    def _route(*a, **k):
        if len(a) == 1 and callable(a[0]) and not k:
            return a[0]

        def wrap(fn):
            return fn

        return wrap

    class _App:
        def __init__(self, *a, **k):
            pass

        def __getattr__(self, name):
            return _route

    fastapi = types.ModuleType("fastapi")
    fastapi.FastAPI = _App
    fastapi.HTTPException = type("HTTPException", (Exception,), {})
    fastapi.Query = lambda default=None, **k: default
    fastapi.UploadFile = object
    fastapi.File = lambda *a, **k: None
    sys.modules["fastapi"] = fastapi

    responses = types.ModuleType("fastapi.responses")
    for name in ("FileResponse", "HTMLResponse", "Response", "StreamingResponse"):
        setattr(responses, name, type(name, (), {}))
    sys.modules["fastapi.responses"] = responses

    static = types.ModuleType("fastapi.staticfiles")
    static.StaticFiles = lambda *a, **k: None
    sys.modules["fastapi.staticfiles"] = static

    pydantic = types.ModuleType("pydantic")

    class BaseModel:
        pass

    pydantic.BaseModel = BaseModel
    pydantic.Field = lambda default=None, **k: default
    sys.modules["pydantic"] = pydantic

    try:
        import yaml  # noqa: F401
    except ImportError:
        yaml = types.ModuleType("yaml")
        yaml.safe_load = lambda *a, **k: {}
        yaml.safe_dump = lambda *a, **k: ""
        sys.modules["yaml"] = yaml


class SnapshotPageTests(unittest.TestCase):
    def test_offset_skips_live_frames_and_keeps_total(self):
        _stub_web_deps()
        import web.app as appmod

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            a = root / "ch_a"
            b = root / "ch_b"
            a.mkdir()
            b.mkdir()
            names = [
                (a, "freeze_1.jpg", 100),
                (a, "black_2.jpg", 300),
                (a, "latest.jpg", 400),
                (a, "latest_ok.jpg", 500),
                (b, "silence_3.jpg", 200),
                (b, "ai_green_screen_4.jpg", 50),
            ]
            for folder, name, ts in names:
                path = folder / name
                path.write_bytes(b"x")
                os.utime(path, (ts, ts))
            old_dir = appmod.SNAPSHOT_DIR
            old_names = appmod._channel_name_map
            appmod.SNAPSHOT_DIR = root
            appmod._channel_name_map = lambda: {"ch_a": "甲", "ch_b": "乙"}
            try:
                page, total = appmod._list_snapshots(limit=2, offset=0)
                self.assertEqual(total, 4)
                self.assertEqual(
                    [row["filename"] for row in page],
                    ["black_2.jpg", "silence_3.jpg"],
                )
                page2, total2 = appmod._list_snapshots(limit=2, offset=2)
                self.assertEqual(total2, 4)
                self.assertEqual(
                    [row["filename"] for row in page2],
                    ["freeze_1.jpg", "ai_green_screen_4.jpg"],
                )
                empty, total3 = appmod._list_snapshots(limit=2, offset=4)
                self.assertEqual(total3, 4)
                self.assertEqual(empty, [])
                one, one_total = appmod._list_snapshots(
                    channel_id="ch_a", limit=10, offset=0
                )
                self.assertEqual(one_total, 2)
                self.assertEqual(one[0]["channel_name"], "甲")
                silence = [row for row in page if row["filename"] == "silence_3.jpg"][0]
                black = [row for row in page if row["filename"] == "black_2.jpg"][0]
                self.assertEqual(silence["audio_state"], "silent")
                self.assertEqual(silence["audio_db"], -120.0)
                self.assertEqual(black["audio_state"], "idle")
                self.assertIsNone(black["audio_db"])
                side = a / "freeze_1.jpg.audio.json"
                side.write_text(
                    '{"audio_db": -12.5, "audio_state": "level"}',
                    encoding="utf-8",
                )
                with_level, _ = appmod._list_snapshots(
                    channel_id="ch_a", limit=10, offset=0
                )
                freeze = [row for row in with_level if row["filename"] == "freeze_1.jpg"][0]
                self.assertEqual(freeze["audio_state"], "level")
                self.assertEqual(freeze["audio_db"], -12.5)
            finally:
                appmod.SNAPSHOT_DIR = old_dir
                appmod._channel_name_map = old_names

    def test_alarm_click_uses_the_picture_taken_after_confirm(self):
        _stub_web_deps()
        import web.app as appmod

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            folder = root / "hd_92"
            folder.mkdir()
            name = "freeze_20261012_120011.jpg"
            (folder / name).write_bytes(b"jpeg")
            (folder / (name + ".audio.json")).write_text(
                '{"audio_db": -8, "audio_state": "level"}',
                encoding="utf-8",
            )
            old_dir = appmod.SNAPSHOT_DIR
            appmod.SNAPSHOT_DIR = root
            try:
                events = [
                    {
                        "type": "freeze",
                        "phase": "start",
                        "channel_id": "hd_92",
                        "channel_name": "新闻",
                        "time": "2026-10-12 12:00:00",
                        "message": "检测到静帧",
                    }
                ]
                appmod._attach_snapshot_urls(events)
                self.assertEqual(events[0]["snapshot"], name)
                self.assertEqual(
                    events[0]["snapshot_url"],
                    "/api/snapshots/hd_92/" + name,
                )
                self.assertEqual(events[0]["audio_db"], -8)
                self.assertEqual(events[0]["audio_state"], "level")
                shots = appmod._card_alarm_shots(
                    {"id": "hd_92", "active_alarms": ["freeze", "no_signal"]},
                    appmod._event_shot_index(events),
                )
                self.assertEqual(shots["freeze"]["file"], name)
                self.assertNotIn("no_signal", shots)
                missing = [
                    {
                        "type": "silence",
                        "channel_id": "hd_92",
                        "time": "2026-10-12 11:00:00",
                        "message": "检测到无伴音",
                    }
                ]
                appmod._attach_snapshot_urls(missing)
                self.assertEqual(missing[0]["snapshot_url"], "")
                self.assertNotIn("snapshot", missing[0])
                (folder / "silence_20261012_120100.jpg").write_bytes(b"jpeg")
                remembered = appmod._card_alarm_shots(
                    {
                        "id": "hd_92",
                        "active_alarms": ["silence"],
                        "open_shots": {
                            "silence": {
                                "file": "silence_20261012_120100.jpg",
                                "time": "2026-10-12 12:00:48",
                                "audio_db": -120,
                                "audio_state": "silent",
                            }
                        },
                    },
                    {},
                )
                self.assertEqual(
                    remembered["silence"]["file"], "silence_20261012_120100.jpg"
                )
                self.assertEqual(remembered["silence"]["audio_state"], "silent")
                stale = appmod._card_alarm_shots(
                    {
                        "id": "hd_92",
                        "active_alarms": ["freeze"],
                        "alarm_since": {"freeze": "2026-10-14 13:05:00"},
                        "open_shots": {
                            "freeze": {
                                "file": "freeze_missing.jpg",
                                "time": "2026-10-14 12:00:00",
                            }
                        },
                    },
                    appmod._event_shot_index(events),
                )
                self.assertEqual(stale["freeze"]["time"], "2026-10-14 13:05:00")
                self.assertEqual(stale["freeze"]["file"], "")
                self.assertNotEqual(stale["freeze"]["file"], name)
            finally:
                appmod.SNAPSHOT_DIR = old_dir


if __name__ == "__main__":
    unittest.main()
