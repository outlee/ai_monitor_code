#!/usr/bin/env python3
# -*- coding: utf-8 -*-
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from web.channel_order import merge_channel_order, sort_cards_by_order  # noqa: E402


class ChannelOrderTests(unittest.TestCase):
    def test_merge_keeps_saved_and_appends_new(self):
        saved = ["b", "a", "gone"]
        current = ["a", "c", "b"]
        self.assertEqual(merge_channel_order(saved, current), ["b", "a", "c"])

    def test_merge_empty_saved_uses_current(self):
        self.assertEqual(merge_channel_order([], ["x", "y"]), ["x", "y"])

    def test_sort_cards_follows_order(self):
        cards = [
            {"id": "a", "name": "甲"},
            {"id": "b", "name": "乙"},
            {"id": "c", "name": "丙"},
        ]
        out = sort_cards_by_order(cards, ["c", "a", "b"])
        self.assertEqual([c["id"] for c in out], ["c", "a", "b"])

    def test_unknown_saved_ids_dropped(self):
        self.assertEqual(merge_channel_order(["z", "a"], ["a"]), ["a"])


if __name__ == "__main__":
    unittest.main()
