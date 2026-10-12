# -*- coding: utf-8 -*-
"""告警截图文件名。大屏实时图 latest.jpg / latest_ok.jpg 不算告警图。"""
from __future__ import annotations

from datetime import datetime


def is_alarm_snapshot(name: str) -> bool:
    if not name or "/" in name or "\\" in name or ".." in name:
        return False
    low = name.strip().lower()
    if not low.endswith(".jpg"):
        return False
    if low.startswith(".") or low.endswith(".part"):
        return False
    if low in ("latest.jpg", "latest_ok.jpg") or low.startswith("latest"):
        return False
    return True


def alarm_jpeg_name(event_type: str, when: str) -> str:
    """由告警类型和时间还原截图文件名。结束和断流没有画面。"""
    kind = str(event_type or "").strip()
    if not kind or kind.endswith("_end") or kind in ("stream_down", "no_signal"):
        return ""
    text = str(when or "").strip()
    if len(text) < 19 or text[4] != "-" or text[10] != " " or text[13] != ":":
        return ""
    stamp = (
        text[0:4] + text[5:7] + text[8:10] + "_" + text[11:13] + text[14:16] + text[17:19]
    )
    name = "%s_%s.jpg" % (kind, stamp)
    if not is_alarm_snapshot(name):
        return ""
    return name


def snapshot_basename(event: dict) -> str:
    raw = str((event or {}).get("snapshot") or "").replace("\\", "/")
    name = raw.split("/")[-1] if raw else ""
    if is_alarm_snapshot(name):
        return name
    return alarm_jpeg_name((event or {}).get("type"), (event or {}).get("time"))


def _event_clock(when: str):
    text = str(when or "").strip()
    if len(text) < 19:
        return None
    try:
        return datetime.strptime(text[:19], "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def _file_clock(name: str):
    """从 `{类型}_YYYYMMDD_HHMMSS.jpg` 取出类型和时间。"""
    if not name or not str(name).lower().endswith(".jpg"):
        return None, ""
    stem = str(name)[:-4]
    if len(stem) < 16 or stem[-16] != "_":
        return None, ""
    stamp = stem[-15:]
    kind = stem[:-16]
    try:
        return datetime.strptime(stamp, "%Y%m%d_%H%M%S"), kind
    except ValueError:
        return None, ""


def pick_alarm_file(names, event_type: str, when: str, snapshot: str = "") -> str:
    """选出这一条告警对应的截图文件名。

    记录里已有的文件名优先。否则用类型加时间。
    确认要过几秒才落图，所以也接受事件时间之后两分半内最近的那张。
    """
    kind = str(event_type or "").strip()
    if not kind or kind.endswith("_end") or kind in ("stream_down", "no_signal"):
        return ""
    have = set(names or [])
    raw = str(snapshot or "").replace("\\", "/").split("/")[-1]
    if raw and is_alarm_snapshot(raw) and raw in have:
        return raw
    exact = alarm_jpeg_name(kind, when)
    if exact and exact in have:
        return exact
    event_at = _event_clock(when)
    best = ""
    best_score = None
    if event_at is not None:
        prefix = kind + "_"
        for name in have:
            if not str(name).startswith(prefix) or not is_alarm_snapshot(name):
                continue
            stamped, file_kind = _file_clock(name)
            if stamped is None:
                continue
            if file_kind != kind and not str(file_kind).startswith(kind + "_"):
                continue
            delta = (stamped - event_at).total_seconds()
            if delta < -2.0 or delta > 150.0:
                continue
            score = abs(delta)
            if best_score is None or score < best_score:
                best = name
                best_score = score
    if best:
        return best
    return exact


def is_proxy_image(name: str) -> bool:
    """汇总页转发：实时图给大屏，告警图给异常截图。"""
    if not name or "/" in name or "\\" in name or ".." in name:
        return False
    low = name.strip().lower()
    if low in ("latest.jpg", "latest_ok.jpg"):
        return True
    return is_alarm_snapshot(name)
