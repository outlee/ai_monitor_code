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
    judge_rgb_alarm,
    rgb_green_ratio,
    rgb_mosaic_ratio,
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
