# -*- coding: utf-8 -*-
"""大屏频道排列：与监测进程分组解耦，避免改顺序重启 Worker。"""
from __future__ import annotations

from typing import Dict, List


def merge_channel_order(saved_ids: List[str], current_ids: List[str]) -> List[str]:
    """保留已保存顺序，新频道接到末尾，去掉已不存在的 id。"""
    have = set(current_ids)
    out = []
    seen = set()
    for cid in saved_ids or []:
        cid = str(cid) if cid is not None else ""
        if cid and cid in have and cid not in seen:
            out.append(cid)
            seen.add(cid)
    for cid in current_ids or []:
        if cid and cid not in seen:
            out.append(cid)
            seen.add(cid)
    return out


def sort_cards_by_order(cards: List[Dict], order_ids: List[str]) -> List[Dict]:
    rank = {cid: i for i, cid in enumerate(order_ids or [])}
    return sorted(
        cards,
        key=lambda c: (
            rank.get(c.get("id"), 10 ** 6),
            c.get("name") or c.get("id") or "",
        ),
    )
