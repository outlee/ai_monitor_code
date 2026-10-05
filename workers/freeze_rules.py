# -*- coding: utf-8 -*-
"""静帧模式与确认秒数。页面上的秒数按这里生效，避免再出现写 8 跑 12。"""
from __future__ import annotations


VIDEO = "video"
VIDEO_SILENCE = "video_silence"


def normalize_freeze_mode(raw) -> str:
    mode = str(raw or VIDEO).strip()
    if mode != VIDEO_SILENCE:
        return VIDEO
    return VIDEO_SILENCE


def effective_freeze_seconds(mode, duration) -> float:
    """只报静帧：最短 12 秒。静帧且无伴音：按填写值，最短 1 秒。"""
    mode = normalize_freeze_mode(mode)
    try:
        d = float(duration)
    except (TypeError, ValueError):
        d = 12.0 if mode == VIDEO else 8.0
    if mode == VIDEO_SILENCE:
        if d < 1.0:
            return 1.0
        return d
    if d < 12.0:
        return 12.0
    return d
