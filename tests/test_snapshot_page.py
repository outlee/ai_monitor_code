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
            finally:
                appmod.SNAPSHOT_DIR = old_dir
                appmod._channel_name_map = old_names


if __name__ == "__main__":
    unittest.main()
