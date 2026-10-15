# -*- coding: utf-8 -*-
"""桌面汇总的纯逻辑。不访问三台监测机。"""
import json
import unittest
from pathlib import Path

from desktop import hub_client, player


class HubClientTests(unittest.TestCase):
    def test_split_uid(self):
        self.assertEqual(hub_client.split_uid("n176/v30_jinying"), ("n176", "v30_jinying"))

    def test_split_uid_rejects_junk(self):
        for bad in ("", "only", "/ch", "n176/", "n176/a/b", "n176/../x", "n176/a..b"):
            with self.assertRaises(ValueError):
                hub_client.split_uid(bad)

    def test_pick_by_id_or_name(self):
        nodes = [
            {"id": "n173", "name": "173", "url": "http://10.6.9.173:8080"},
            {"id": "n176", "name": "176", "url": "http://10.6.9.176:8080"},
        ]
        self.assertEqual(len(hub_client.pick_nodes(nodes, "all")), 2)
        self.assertEqual(hub_client.pick_nodes(nodes, "176")[0]["id"], "n176")
        self.assertEqual(hub_client.pick_nodes(nodes, "n173")[0]["name"], "173")
        with self.assertRaises(ValueError):
            hub_client.pick_nodes(nodes, "199")

    def test_rewrite_snap(self):
        self.assertEqual(
            hub_client.rewrite_snap_url("n177", "/api/snapshots/hd_1/freeze_1.jpg"),
            "/api/hub/snap/n177/hd_1/freeze_1.jpg",
        )
        self.assertEqual(hub_client.rewrite_snap_url("n177", ""), "")

    def test_apply_order_keeps_saved_and_appends_new(self):
        saved = ["n173/a", "gone", "n176/b"]
        current = ["n176/b", "n177/c", "n173/a"]
        self.assertEqual(
            hub_client.apply_order(saved, current),
            ["n173/a", "n176/b", "n177/c"],
        )

    def test_order_roundtrip(self):
        path = Path("/tmp/am_desktop_order_test.json")
        if path.exists():
            path.unlink()
        self.assertEqual(hub_client.read_order(path), [])
        hub_client.write_order(path, ["n173/a", "n176/b"])
        self.assertEqual(hub_client.read_order(path), ["n173/a", "n176/b"])
        path.unlink()

    def test_nodes_roundtrip(self):
        path = Path("/tmp/am_desktop_nodes_test.json")
        nodes = hub_client.load_nodes_from_payload(
            {"nodes": [{"id": "n173", "name": "173", "url": "http://10.6.9.173:8080/"}]}
        )
        self.assertEqual(nodes[0]["url"], "http://10.6.9.173:8080")
        hub_client.save_nodes(path, nodes)
        self.assertEqual(hub_client.load_nodes(path)[0]["id"], "n173")
        path.unlink()
        with self.assertRaises(ValueError):
            hub_client.load_nodes_from_payload({"nodes": [{"id": "n173", "name": "173", "url": ""}]})

    def test_error_text_reads_detail(self):
        class Boom(Exception):
            def read(self):
                return json.dumps({"detail": "模型文件大小不对"}).encode("utf-8")

        self.assertEqual(hub_client._error_text(Boom()), "模型文件大小不对")


class PlayerTests(unittest.TestCase):
    def test_parse_mcast(self):
        self.assertEqual(player.parse_mcast("udp://@239.100.3.1:5000"), ("239.100.3.1", "5000"))
        self.assertEqual(player.parse_mcast("udp://239.1.1.1:1234"), ("239.1.1.1", "1234"))
        self.assertEqual(player.parse_mcast(""), ("", ""))
        self.assertEqual(player.parse_mcast("http://10.6.9.173:8080/api/preview/hd_92"), ("", ""))

    def test_idle_player_is_not_running(self):
        self.assertFalse(player.is_running())

    def test_bundled_file_wins(self):
        root = Path("/tmp/am_desktop_player_test")
        bundled = root / "player" / "ffplay"
        bundled.parent.mkdir(parents=True, exist_ok=True)
        bundled.write_text("not-a-real-binary", encoding="utf-8")
        try:
            self.assertEqual(player.player_command(root), [str(bundled)])
        finally:
            bundled.unlink()


class ServerGuardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            from fastapi.testclient import TestClient
            from desktop.server import app
        except Exception as exc:
            raise unittest.SkipTest("fastapi 不可用: %s" % exc)
        cls.client = TestClient(app)

    def test_play_status_idle(self):
        res = self.client.get("/api/desktop/play")
        self.assertEqual(res.status_code, 200)
        self.assertFalse(res.json().get("playing"))

    def test_page_injects_desktop_script(self):
        res = self.client.get("/")
        self.assertEqual(res.status_code, 200)
        self.assertIn("/desktop/desktop.js", res.text)
        self.assertIn("/static/app.js", res.text)

    def test_all_scope_rejects_channel_writes(self):
        created = self.client.post(
            "/api/config/channels?scope=all",
            json={"id": "tmp", "name": "tmp", "url": "udp://@239.1.1.1:5000"},
        )
        cleared = self.client.post("/api/storage/clear?scope=all", json={"snapshots": True})
        imported = self.client.post(
            "/api/config/import?scope=all",
            json={"mode": "merge", "channels": []},
        )
        exported = self.client.get("/api/config/export?fmt=json&scope=all")
        for res in (created, cleared, imported, exported):
            self.assertEqual(res.status_code, 400, res.text)
            self.assertIn("只选一台", res.json()["detail"])

    def test_tiny_model_rejected_before_any_host(self):
        res = self.client.post(
            "/api/desktop/model?scope=all&restart=false",
            files={
                "onnx": ("mosaic_detector.onnx", b"x", "application/octet-stream"),
                "classes": (
                    "mosaic_detector.onnx.classes.json",
                    b'{"classes":["normal"]}',
                    "application/json",
                ),
            },
        )
        self.assertEqual(res.status_code, 400)
        self.assertIn("大小", res.json()["detail"])

    def test_bad_classes_rejected(self):
        res = self.client.post(
            "/api/desktop/model?scope=n173&restart=false",
            files={
                "onnx": ("mosaic_detector.onnx", b"y" * 1200, "application/octet-stream"),
                "classes": ("mosaic_detector.onnx.classes.json", b"[]", "application/json"),
            },
        )
        self.assertEqual(res.status_code, 400)
        self.assertIn("classes", res.json()["detail"])


if __name__ == "__main__":
    unittest.main()
