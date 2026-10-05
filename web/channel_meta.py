# -*- coding: utf-8 -*-
"""频道分类。手选标签，不按节目名猜测。"""
from __future__ import annotations

PRESET_CATEGORIES = ("央视", "卫视", "高清", "标清")


def clean_category(raw) -> str:
    if raw is None:
        return ""
    text = str(raw).strip()
    if not text:
        return ""
    if any(ch in text for ch in "/\\\n\r\t"):
        raise ValueError("分类不能含斜杠或换行")
    if len(text) > 32:
        text = text[:32]
    return text
