# -*- coding: utf-8 -*-
"""告警截图文件名。大屏实时图 latest.jpg / latest_ok.jpg 不算告警图。"""
from __future__ import annotations


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


def is_proxy_image(name: str) -> bool:
    """汇总页转发：实时图给大屏，告警图给异常截图。"""
    if not name or "/" in name or "\\" in name or ".." in name:
        return False
    low = name.strip().lower()
    if low in ("latest.jpg", "latest_ok.jpg"):
        return True
    return is_alarm_snapshot(name)
