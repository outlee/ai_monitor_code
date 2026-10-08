# -*- coding: utf-8 -*-
"""花屏 / 马赛克的原始 RGB 判定，以及 onnx 失败后改用内置检测。"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "workers"))

from ai_detector import (  # noqa: E402
    AIDetector,
    decide_picture_alarm,
    judge_rgb_alarm,
    mosaic_ratio_hwc,
    rgb_green_ratio,
    rgb_mosaic_ratio,
)
from frame_quality import (  # noqa: E402
    _judge_rgb,
    concealment_rgb_bytes,
    jpeg_looks_displayable,
    pure_green_ratio_bytes,
)


def _solid(w, h, rgb):
    r, g, b = rgb
    return bytes([r, g, b]) * (w * h)


def _checker(w, h, block=8):
    buf = bytearray(w * h * 3)
    for y in range(h):
        for x in range(w):
            on = ((x // block) + (y // block)) % 2 == 0
            v = 255 if on else 0
            i = (y * w + x) * 3
            buf[i] = buf[i + 1] = buf[i + 2] = v
    return bytes(buf)


def _noise(w, h, seed=3):
    buf = bytearray(w * h * 3)
    x = seed
    for i in range(len(buf)):
        x = (1103515245 * x + 12345) & 0x7FFFFFFF
        buf[i] = x & 255
    return bytes(buf)


class RgbAlarmTests(unittest.TestCase):
    def test_flat_and_noise_are_not_mosaic(self):
        self.assertAlmostEqual(rgb_mosaic_ratio(_solid(32, 32, (40, 40, 40)), 32, 32), 0.0)
        self.assertLess(rgb_mosaic_ratio(_noise(32, 32), 32, 32), 0.15)

    def test_checkerboard_is_mosaic(self):
        ratio = rgb_mosaic_ratio(_checker(32, 32), 32, 32)
        self.assertGreater(ratio, 0.9)
        judged = judge_rgb_alarm(_checker(32, 32), 32, 32)
        self.assertEqual(judged["label"], "mosaic")
        self.assertTrue(judged["is_anomaly"])

    def test_green_screen(self):
        buf = _solid(16, 16, (10, 220, 10))
        self.assertGreater(rgb_green_ratio(buf, 16, 16), 0.9)
        judged = judge_rgb_alarm(buf, 16, 16)
        self.assertEqual(judged["label"], "green_screen")

    def test_ordinary_picture_is_normal(self):
        judged = judge_rgb_alarm(_solid(32, 32, (80, 90, 70)), 32, 32)
        self.assertEqual(judged["label"], "normal")
        self.assertFalse(judged["is_anomaly"])


class DecidePictureTests(unittest.TestCase):
    def test_high_onnx_score_on_a_normal_frame_is_not_mosaic(self):
        # 线上 CCTV-13 / 少儿动画的分数就在这个区间，画面本身没有方块。
        judged = decide_picture_alarm(0.0, 0.014, onnx_score=0.87)
        self.assertEqual(judged["label"], "normal")
        self.assertFalse(judged["is_anomaly"])

    def test_real_blocks_still_alarm(self):
        judged = decide_picture_alarm(0.0, 0.62, onnx_score=0.2)
        self.assertEqual(judged["label"], "mosaic")
        self.assertTrue(judged["is_anomaly"])

    def test_green_does_not_need_the_model(self):
        judged = decide_picture_alarm(0.8, 0.0, onnx_score=0.1)
        self.assertEqual(judged["label"], "green_screen")

    def test_small_pure_green_is_green_screen(self):
        judged = decide_picture_alarm(0.0, 0.0, pure_ratio=0.01)
        self.assertEqual(judged["label"], "green_screen")
        self.assertTrue(judged["is_anomaly"])

    def test_greenish_picture_without_pure_green_is_normal(self):
        judged = decide_picture_alarm(0.2, 0.0, pure_ratio=0.0)
        self.assertEqual(judged["label"], "normal")
        self.assertFalse(judged["is_anomaly"])


def _picture_with_green_bar(w, h, bar_rows):
    buf = bytearray(_noise(w, h, seed=7))
    if bar_rows <= 0:
        return bytes(buf)
    start = (h - bar_rows) * w
    pix = b"\x0a\xdc\x0a"
    for i in range(start, w * h):
        o = i * 3
        buf[o : o + 3] = pix
    return bytes(buf)


class ConcealmentTests(unittest.TestCase):
    def test_green_bar_stays_off_the_wall_and_counts_as_green(self):
        # 宽纯绿横条不上大屏。抽不到正常画面时，这张图按绿屏告警。
        buf = _picture_with_green_bar(160, 90, 36)
        self.assertGreater(rgb_green_ratio(buf, 160, 90), 0.35)
        self.assertTrue(concealment_rgb_bytes(buf, 160, 90))
        judged = judge_rgb_alarm(buf, 160, 90)
        self.assertEqual(judged["label"], "green_screen")
        self.assertTrue(judged["is_anomaly"])
        ok, reason = _judge_rgb(buf, 160, 90)
        self.assertFalse(ok)
        self.assertEqual(reason, "conceal")

    def test_small_pure_patch_alarms_and_can_stay_on_screen(self):
        buf = bytearray(_noise(160, 90, seed=5))
        pix = b"\x0a\xdc\x0a"
        for y in range(24):
            for x in range(24):
                o = (y * 160 + x) * 3
                buf[o : o + 3] = pix
        raw = bytes(buf)
        self.assertGreater(pure_green_ratio_bytes(raw, 160, 90), 0.004)
        self.assertFalse(concealment_rgb_bytes(raw, 160, 90))
        judged = judge_rgb_alarm(raw, 160, 90)
        self.assertEqual(judged["label"], "green_screen")
        ok, reason = _judge_rgb(raw, 160, 90)
        self.assertTrue(ok)
        self.assertEqual(reason, "ok")
        self.assertLess(pure_green_ratio_bytes(_noise(160, 90, seed=9), 160, 90), 0.00001)

    def test_full_green_is_still_huaping(self):
        buf = _solid(96, 54, (10, 220, 10))
        self.assertFalse(concealment_rgb_bytes(buf, 96, 54))
        judged = judge_rgb_alarm(buf, 96, 54)
        self.assertEqual(judged["label"], "green_screen")
        ok, reason = _judge_rgb(buf, 96, 54)
        self.assertFalse(ok)
        self.assertEqual(reason, "green")

    def test_plain_picture_is_shown(self):
        buf = _noise(96, 54, seed=11)
        self.assertFalse(concealment_rgb_bytes(buf, 96, 54))
        ok, reason = _judge_rgb(buf, 96, 54)
        self.assertTrue(ok)
        self.assertEqual(reason, "ok")

    def test_jpeg_bar_rejected_and_full_green_kept_for_ai(self):
        try:
            from PIL import Image
            import io
        except ImportError:
            self.skipTest("no PIL")

        def _jpeg(buf, w, h):
            im = Image.frombytes("RGB", (w, h), buf)
            out = io.BytesIO()
            im.save(out, format="JPEG", quality=85)
            return out.getvalue()

        bar = _jpeg(_picture_with_green_bar(320, 180, 70), 320, 180)
        ok, reason = jpeg_looks_displayable(bar, min_bytes=64)
        self.assertFalse(ok)
        self.assertEqual(reason, "conceal")
        solid = _jpeg(_solid(160, 90, (10, 220, 10)), 160, 90)
        ok, reason = jpeg_looks_displayable(solid, min_bytes=64)
        self.assertFalse(ok)
        self.assertEqual(reason, "green")


class NativeBlockTests(unittest.TestCase):
    def test_sixteen_pixel_tiles_count_as_mosaic(self):
        import numpy as np

        rng = np.random.RandomState(1)
        bh, bw, block = 8, 10, 16
        colors = rng.randint(0, 256, size=(bh, bw, 3), dtype=np.uint8)
        img = np.repeat(np.repeat(colors, block, axis=0), block, axis=1)
        ratio = mosaic_ratio_hwc(img)
        self.assertGreater(ratio, 0.5)
        judged = decide_picture_alarm(0.0, ratio, onnx_score=0.9)
        self.assertEqual(judged["label"], "mosaic")

    def test_large_flat_regions_are_not_mosaic(self):
        import numpy as np

        img = np.zeros((128, 160, 3), dtype=np.uint8)
        img[:64] = 255
        self.assertLess(mosaic_ratio_hwc(img), 0.2)
        judged = decide_picture_alarm(0.0, mosaic_ratio_hwc(img), onnx_score=0.87)
        self.assertEqual(judged["label"], "normal")


class BackendFallbackTests(unittest.TestCase):
    def test_onnx_mode_without_model_uses_builtin(self):
        det = AIDetector(
            {"enabled": True, "mode": "onnx", "model_path": "models/missing.onnx"},
            work_dir=str(ROOT),
        )
        self.assertTrue(det.available)
        self.assertEqual(det.backend, "builtin")
        self.assertGreaterEqual(det.confirm_sec, 1.0)

    def test_disabled_stays_off(self):
        det = AIDetector({"enabled": False, "mode": "onnx"}, work_dir=str(ROOT))
        self.assertFalse(det.available)


if __name__ == "__main__":
    unittest.main()
