# -*- coding: utf-8 -*-
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "workers"))

from ai_detector import (  # noqa: E402
    _load_class_names,
    interpret_class_output,
    uses_named_class_output,
)
from picture_classes import (  # noqa: E402
    FRAME_LABEL_ZH,
    class_folders,
    discover_frame_classes,
    select_frame_classes,
)


class SelectClassTests(unittest.TestCase):
    def test_legacy_binary_stays_two_class(self):
        names, skipped = select_frame_classes(["normal", "anomaly"])
        self.assertEqual(names, ["normal", "anomaly"])
        self.assertEqual(skipped, [])

    def test_present_faults_keep_frame_order(self):
        names, skipped = select_frame_classes(["solid_green", "normal", "black"])
        self.assertEqual(names, ["normal", "black", "solid_green"])
        self.assertEqual(skipped, [])

    def test_green_screen_and_mosaic_are_one_class(self):
        names, skipped = select_frame_classes(
            ["mosaic", "normal", "green_screen"]
        )
        self.assertEqual(names, ["normal", "green_screen_mosaic"])
        self.assertEqual(skipped, [])
        self.assertEqual(FRAME_LABEL_ZH["green_screen_mosaic"], "花屏马赛克")

    def test_temporal_folders_are_skipped(self):
        names, skipped = select_frame_classes(["normal", "black", "freeze", "flicker"])
        self.assertEqual(names, ["normal", "black"])
        self.assertEqual(skipped, ["freeze", "flicker"])

    def test_anomaly_folder_cannot_mix_with_named_faults(self):
        with self.assertRaises(ValueError):
            select_frame_classes(["normal", "anomaly", "mosaic"])

    def test_snow_is_left_out_and_solid_green_is_its_own_class(self):
        names, skipped = select_frame_classes(
            ["normal", "snow", "solid_green", "green_screen"]
        )
        self.assertEqual(names, ["normal", "solid_green", "green_screen_mosaic"])
        self.assertEqual(skipped, [])
        self.assertEqual(FRAME_LABEL_ZH["solid_green"], "绿屏")
        self.assertEqual(FRAME_LABEL_ZH["green_screen"], "花屏")
        self.assertNotIn("snow", FRAME_LABEL_ZH)

    def test_color_cast_and_exposure_are_one_class(self):
        names, skipped = select_frame_classes(
            ["normal", "underexposed", "color_cast", "overexposed", "inverted"]
        )
        self.assertEqual(names, ["normal", "color_exposure", "inverted"])
        self.assertEqual(skipped, [])
        self.assertEqual(FRAME_LABEL_ZH["color_exposure"], "偏色曝光")
        self.assertEqual(
            class_folders("color_exposure"),
            ["color_exposure", "color_cast", "overexposed", "underexposed"],
        )

    def test_color_bars_count_as_normal(self):
        names, skipped = select_frame_classes(
            ["normal", "color_bars", "solid_green"]
        )
        self.assertEqual(names, ["normal", "solid_green"])
        self.assertEqual(skipped, [])
        self.assertNotIn("color_bars", FRAME_LABEL_ZH)

    def test_unknown_folder_is_not_a_class(self):
        names, skipped = select_frame_classes(
            ["normal", "mosaic", "logo_missing", "freeze"]
        )
        self.assertEqual(names, ["normal", "green_screen_mosaic"])
        self.assertEqual(skipped, ["freeze"])


class DiscoverFolderTests(unittest.TestCase):
    def test_empty_and_temporal_folders_do_not_add_a_class(self):
        import tempfile

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "normal").mkdir()
            (root / "normal" / "a.jpg").write_bytes(b"\xff\xd8")
            (root / "mosaic").mkdir()
            (root / "mosaic" / "m.jpg").write_bytes(b"\xff\xd8")
            (root / "black").mkdir()
            (root / "freeze").mkdir()
            (root / "freeze" / "b.jpg").write_bytes(b"\xff\xd8")
            (root / "not_a_class").mkdir()
            (root / "not_a_class" / "c.jpg").write_bytes(b"\xff\xd8")
            classes, skipped, unknown = discover_frame_classes(root)
        self.assertEqual(classes, ["normal", "green_screen_mosaic"])
        self.assertEqual(skipped, ["freeze"])
        self.assertEqual(unknown, ["not_a_class"])


class InterpretClassTests(unittest.TestCase):
    def test_class_alarm_off_does_not_raise(self):
        judged = interpret_class_output(
            [0.1, 0.8, 0.1],
            ["normal", "mosaic", "green_screen"],
            class_alarm=False,
            threshold=0.55,
        )
        self.assertFalse(judged["is_anomaly"])
        self.assertEqual(judged["label"], "normal")
        self.assertEqual(judged["detail"]["top_class"], "mosaic")

    def test_class_alarm_on_uses_the_top_fault(self):
        judged = interpret_class_output(
            [0.1, 0.2, 0.7],
            ["normal", "mosaic", "green_screen"],
            class_alarm=True,
            threshold=0.55,
        )
        self.assertTrue(judged["is_anomaly"])
        self.assertEqual(judged["label"], "green_screen")

    def test_low_score_stays_normal(self):
        judged = interpret_class_output(
            [0.4, 0.5, 0.1],
            ["normal", "black", "mosaic"],
            class_alarm=True,
            threshold=0.55,
        )
        self.assertFalse(judged["is_anomaly"])
        self.assertEqual(judged["label"], "normal")

    def test_length_mismatch_is_unknown(self):
        judged = interpret_class_output([0.2, 0.8], ["normal"], True, 0.5)
        self.assertFalse(judged["is_anomaly"])
        self.assertEqual(judged["label"], "unknown")


class ClassFileTests(unittest.TestCase):
    def test_binary_without_sidecar_stays_two_names(self):
        names = _load_class_names(Path("/tmp/does_not_exist_mosaic.onnx"), 2)
        self.assertEqual(names, ["normal", "anomaly"])

    def test_wide_output_without_sidecar_is_ignored(self):
        names = _load_class_names(Path("/tmp/does_not_exist_wide.onnx"), 4)
        self.assertIsNone(names)

    def test_sidecar_must_match_width(self):
        import json
        import tempfile

        with tempfile.TemporaryDirectory() as folder:
            model = Path(folder) / "mosaic_detector.onnx"
            model.write_bytes(b"")
            side = Path(str(model) + ".classes.json")
            side.write_text(
                json.dumps({"classes": ["normal", "green_screen", "mosaic"]}),
                encoding="utf-8",
            )
            self.assertEqual(
                _load_class_names(model, 3),
                ["normal", "green_screen", "mosaic"],
            )
            self.assertIsNone(_load_class_names(model, 2))


class NamedClassOutputTests(unittest.TestCase):
    def test_legacy_binary_score_is_not_a_class(self):
        self.assertFalse(uses_named_class_output(["normal", "anomaly"], 2))
        self.assertFalse(uses_named_class_output(None, 2))
        self.assertFalse(uses_named_class_output(["normal", "green_screen_mosaic"], 3))

    def test_two_class_fault_model_uses_class_output(self):
        self.assertTrue(
            uses_named_class_output(["normal", "green_screen_mosaic"], 2)
        )
        self.assertTrue(
            uses_named_class_output(
                ["normal", "solid_green", "green_screen_mosaic"], 3
            )
        )


if __name__ == "__main__":
    unittest.main()
