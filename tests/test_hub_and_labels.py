# -*- coding: utf-8 -*-
"""汇总节点、告警截图文件名、分类标签。"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "web"))
sys.path.insert(0, str(ROOT / "workers"))

from channel_meta import clean_categories, clean_category, resolve_categories  # noqa: E402
from freeze_rules import effective_freeze_seconds  # noqa: E402
from hub import assemble_hub, assemble_perf, hub_is_active, normalize_node_list  # noqa: E402
from monitor_worker import (  # noqa: E402
    alarm_ring_tail,
    alarm_tail_nbytes,
    alarm_tail_span_sec,
    alarm_tail_spans,
)
from snapshot_names import is_alarm_snapshot  # noqa: E402


class SnapshotNameTests(unittest.TestCase):
    def test_live_frames_are_not_alarms(self):
        self.assertFalse(is_alarm_snapshot("latest.jpg"))
        self.assertFalse(is_alarm_snapshot("latest_ok.jpg"))
        self.assertFalse(is_alarm_snapshot(".latest_hd_98.tmp.jpg"))

    def test_alarm_files(self):
        self.assertTrue(is_alarm_snapshot("freeze_20260928_164916.jpg"))
        self.assertTrue(is_alarm_snapshot("black_20260729_091942.jpg"))


class FreezeSecondsTests(unittest.TestCase):
    def test_typed_seconds_for_both_modes(self):
        self.assertEqual(effective_freeze_seconds("video", 8), 8.0)
        self.assertEqual(effective_freeze_seconds("video_silence", 8), 8.0)
        self.assertEqual(effective_freeze_seconds("video", 0.2), 0.5)
        self.assertEqual(effective_freeze_seconds("video_silence", 0.2), 0.5)


class CategoryTests(unittest.TestCase):
    def test_clean(self):
        self.assertEqual(clean_category(" 央视 "), "央视")
        self.assertEqual(clean_category(""), "")
        with self.assertRaises(ValueError):
            clean_category("a/b")

    def test_several_tags(self):
        self.assertEqual(clean_categories("卫视、高清"), ["卫视", "高清"])
        self.assertEqual(clean_categories(["卫视", "高清", "卫视"]), ["卫视", "高清"])
        self.assertEqual(clean_categories("卫视"), ["卫视"])
        self.assertEqual(clean_categories(""), [])

    def test_saved_tags_override_old_string(self):
        ch = {"id": "hd_1", "category": "卫视"}
        self.assertEqual(resolve_categories(ch, {}), ["卫视"])
        self.assertEqual(resolve_categories(ch, {"hd_1": ["卫视", "高清"]}), ["卫视", "高清"])
        self.assertEqual(resolve_categories(ch, {"hd_1": []}), [])


class HubTests(unittest.TestCase):
    def test_inactive_without_remote(self):
        nodes = normalize_node_list([{"id": "local", "name": "本机", "url": ""}])
        self.assertFalse(hub_is_active(nodes))

    def test_merge_remote_and_skip_dead_node(self):
        nodes = normalize_node_list(
            [
                {"id": "local", "name": "本机", "url": ""},
                {"id": "room2", "name": "机房2", "url": "http://10.0.0.8:8080"},
                {"id": "room3", "name": "机房3", "url": "http://10.0.0.9:8080"},
            ]
        )
        local = {
            "cards": [
                {
                    "id": "hd_92",
                    "name": "甘肃卫视",
                    "lamp": "green",
                    "enabled": True,
                    "category": "卫视",
                    "thumb_url": "/api/snapshots/hd_92/latest_ok.jpg?t=1",
                }
            ],
            "recent_events": [],
            "stats_24h": {"total": 1, "by_type": [{"type": "freeze", "count": 1}], "by_channel": []},
        }

        def fetch(url, path):
            if "10.0.0.9" in url:
                raise OSError("down")
            self.assertTrue(path.startswith("/api/dashboard"))
            return {
                "cards": [
                    {
                        "id": "hd_1",
                        "name": "央视一套",
                        "lamp": "red",
                        "enabled": True,
                        "category": "央视",
                        "thumb_url": "/api/snapshots/hd_1/latest_ok.jpg?t=2",
                    }
                ],
                "recent_events": [{"type": "freeze", "channel_id": "hd_1", "message": "检测到静帧"}],
                "stats_24h": {"total": 2, "by_type": [{"type": "freeze", "count": 2}], "by_channel": []},
            }

        merged = assemble_hub(nodes, local, fetch)
        ids = [c["id"] for c in merged["cards"]]
        self.assertEqual(ids, ["local/hd_92", "room2/hd_1"])
        remote = merged["cards"][1]
        self.assertTrue(remote["thumb_url"].startswith("/api/hub/snap/room2/hd_1/"))
        self.assertEqual(remote["preview_base"], "/api/hub/snap/room2/hd_1")
        dead = [n for n in merged["nodes"] if n["id"] == "room3"][0]
        self.assertFalse(dead["ok"])
        self.assertEqual(merged["summary"]["green"], 1)
        self.assertEqual(merged["summary"]["red"], 1)
        self.assertEqual(merged["stats_24h"]["total"], 3)
        self.assertEqual(merged["recent_events"][0]["node_name"], "机房2")

    def test_alarm_tail_stays_inside_the_anomaly(self):
        # 静帧确认时已经持续约 12 秒。截图只取尾部两三秒，不能把整段缓冲头部的正常节目解进去。
        span = alarm_tail_span_sec(12)
        self.assertGreaterEqual(span, 1.0)
        self.assertLessEqual(span, 2.0)
        n = alarm_tail_nbytes(8000, span)
        covered = n * 8.0 / (8000 * 1000.0)
        self.assertGreater(covered, 0.8)
        self.assertLess(covered, 2.5)
        data = b"\x00" * (n + 64) + b"\x11" * n
        self.assertEqual(alarm_ring_tail(data, 8000, span), b"\x11" * n)

        # 黑场确认至少约 5 秒。2Mbps 时尾部仍要比这段黑场短。
        black_span = alarm_tail_span_sec(5)
        black_n = alarm_tail_nbytes(2000, black_span)
        black_covered = black_n * 8.0 / (2000 * 1000.0)
        self.assertLess(black_covered, 5.0)
        for wider in alarm_tail_spans(12):
            wide_n = alarm_tail_nbytes(2000, wider)
            wide_covered = wide_n * 8.0 / (2000 * 1000.0)
            self.assertLess(wide_covered, 12.0)
        self.assertGreaterEqual(len(alarm_tail_spans(12)), 2)

        # 不知道码率时按偏低码率估，避免把低码率节目的正常画面卷进来。
        unknown = alarm_tail_nbytes(0, 2.0)
        self.assertLess(unknown * 8.0 / (2000 * 1000.0), 3.0)

    def test_perf_keeps_dead_remote(self):
        nodes = normalize_node_list(
            [
                {"id": "local", "name": "本机", "url": ""},
                {"id": "room2", "name": "机房2", "url": "http://10.0.0.8:8080"},
                {"id": "room3", "name": "机房3", "url": "http://10.0.0.9:8080"},
            ]
        )

        def fetch(url, path):
            self.assertEqual(path, "/api/system/perf")
            if "10.0.0.9" in url:
                raise OSError("down")
            return {"cpu_percent": 20}

        out = assemble_perf(nodes, {"cpu_percent": 10}, fetch)
        self.assertTrue(out["active"])
        self.assertEqual([n["id"] for n in out["nodes"]], ["local", "room2", "room3"])
        self.assertEqual(out["nodes"][0]["perf"]["cpu_percent"], 10)
        self.assertFalse(out["nodes"][2]["ok"])
        self.assertIsNone(out["nodes"][2]["perf"])

    def test_perf_single_host_without_nodes(self):
        out = assemble_perf([], {"cpu_percent": 1}, lambda *_a: None)
        self.assertFalse(out["active"])
        self.assertEqual(out["nodes"][0]["name"], "本机")
        self.assertTrue(out["nodes"][0]["ok"])

    def test_reject_two_local_nodes(self):
        with self.assertRaises(ValueError):
            normalize_node_list(
                [
                    {"id": "a", "name": "甲", "url": ""},
                    {"id": "b", "name": "乙", "url": ""},
                ]
            )


if __name__ == "__main__":
    unittest.main()