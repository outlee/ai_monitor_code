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

from channel_meta import clean_category  # noqa: E402
from freeze_rules import effective_freeze_seconds  # noqa: E402
from hub import assemble_hub, hub_is_active, normalize_node_list  # noqa: E402
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
    def test_video_floor_and_silence_mode(self):
        self.assertEqual(effective_freeze_seconds("video", 8), 12.0)
        self.assertEqual(effective_freeze_seconds("video_silence", 8), 8.0)
        self.assertEqual(effective_freeze_seconds("video_silence", 0.2), 1.0)


class CategoryTests(unittest.TestCase):
    def test_clean(self):
        self.assertEqual(clean_category(" 央视 "), "央视")
        self.assertEqual(clean_category(""), "")
        with self.assertRaises(ValueError):
            clean_category("a/b")


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