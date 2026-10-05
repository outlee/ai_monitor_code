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
    """两种静帧模式都按填写秒数生效，只限制在 0.5～120 秒。"""
    normalize_freeze_mode(mode)
    try:
        d = float(duration)
    except (TypeError, ValueError):
        d = 12.0
    if d < 0.5:
        return 0.5
    if d > 120.0:
        return 120.0
    return d
