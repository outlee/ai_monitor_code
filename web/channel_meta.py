# -*- coding: utf-8 -*-
"""频道标签。一套节目可以同时是卫视和高清，不按节目名猜测。"""
from __future__ import annotations

import re

PRESET_CATEGORIES = ("央视", "卫视", "高清", "标清")
_SPLIT = re.compile(r"[、,，;；\s]+")


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


def resolve_categories(ch, cat_map) -> list:
    """页面上的标签。单独存过的优先；没有则沿用频道配置里的旧字符串。"""
    cid = str((ch or {}).get("id") or "")
    if cid and isinstance(cat_map, dict) and cid in cat_map:
        try:
            return clean_categories(cat_map[cid])
        except ValueError:
            return []
    try:
        return clean_categories((ch or {}).get("category"))
    except ValueError:
        return []


def clean_categories(raw) -> list:
    """字符串、顿号分隔或列表都收成去重后的标签。旧的单个字符串仍然认。"""
    if raw is None:
        return []
    if isinstance(raw, str):
        parts = [p for p in _SPLIT.split(raw.strip()) if p]
    elif isinstance(raw, (list, tuple)):
        parts = []
        for item in raw:
            if isinstance(item, str):
                parts.extend(p for p in _SPLIT.split(item.strip()) if p)
            elif item is not None:
                raise ValueError("分类必须是文字")
    else:
        raise ValueError("分类必须是文字")
    out = []
    seen = set()
    for part in parts:
        text = clean_category(part)
        if not text or text in seen:
            continue
        seen.add(text)
        out.append(text)
        if len(out) >= 8:
            break
    return out
