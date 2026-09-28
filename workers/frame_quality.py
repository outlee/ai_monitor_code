#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Reject decode-garbage JPEGs (green screen / flat gray) for the dashboard."""

from __future__ import print_function

import subprocess
from pathlib import Path

_cache = {}  # path -> (mtime, size, ok)


def jpeg_looks_displayable(data, min_bytes=2048):
    """
    Return (ok, reason).
    ok=False for green-screen decode errors and low-contrast gray wash.
    Real black (very dark) is allowed so 黑场 still shows.
    """
    if not data or len(data) < int(min_bytes):
        return False, "too_small"
    if data[:2] != b"\xff\xd8" or data.rfind(b"\xff\xd9") < 2:
        return False, "not_jpeg"
    rgb = _decode_rgb_small(data)
    if rgb is None:
        # 解不开像素时不当绿灯，避免半截图上屏
        return False, "undecodable"
    buf, w, h = rgb
    return _judge_rgb(buf, w, h)


def path_looks_displayable(path):
    p = Path(path)
    try:
        st = p.stat()
    except OSError:
        return False
    key = str(p)
    hit = _cache.get(key)
    if hit and hit[0] == st.st_mtime and hit[1] == st.st_size:
        return hit[2]
    try:
        data = p.read_bytes()
    except OSError:
        _cache[key] = (st.st_mtime, st.st_size, False)
        return False
    ok, _reason = jpeg_looks_displayable(data)
    _cache[key] = (st.st_mtime, st.st_size, ok)
    if len(_cache) > 400:
        _cache.clear()
    return ok


def _decode_rgb_small(data):
    try:
        import cv2
        import numpy as np

        arr = np.frombuffer(data, dtype=np.uint8)
        img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if img is None:
            return None
        img = cv2.resize(img, (96, 54))
        img = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
        return bytes(img.tobytes()), 96, 54
    except Exception:
        pass
    try:
        from PIL import Image
        import io

        im = Image.open(io.BytesIO(data)).convert("RGB")
        im.thumbnail((96, 54))
        w, h = im.size
        return im.tobytes(), w, h
    except Exception:
        pass
    try:
        r = subprocess.run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-i",
                "pipe:0",
                "-vf",
                "scale=96:54",
                "-frames:v",
                "1",
                "-f",
                "rawvideo",
                "-pix_fmt",
                "rgb24",
                "pipe:1",
            ],
            input=data,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=3,
        )
        raw = r.stdout or b""
        need = 96 * 54 * 3
        if len(raw) < need:
            return None
        return raw[:need], 96, 54
    except Exception:
        return None


def _judge_rgb(buf, w, h):
    n = int(w) * int(h)
    if n <= 0 or len(buf) < n * 3:
        return False, "bad_rgb"
    green_n = 0
    lum_sum = 0
    lum_sq = 0
    sat_sum = 0
    i = 0
    lim = n * 3
    while i < lim:
        r = buf[i]
        g = buf[i + 1]
        b = buf[i + 2]
        if g > r + 40 and g > b + 40 and g > 70:
            green_n += 1
        y = (r * 3 + g * 6 + b) // 10
        lum_sum += y
        lum_sq += y * y
        mx = r if r > g else g
        if b > mx:
            mx = b
        mn = r if r < g else g
        if b < mn:
            mn = b
        sat_sum += mx - mn
        i += 3
    nf = float(n)
    green_ratio = green_n / nf
    mean = lum_sum / nf
    var = lum_sq / nf - mean * mean
    std = var ** 0.5 if var > 0 else 0.0
    sat = sat_sum / nf
    if green_ratio >= 0.50:
        return False, "green"
    # 真黑场：很暗，允许上屏
    if mean < 22:
        return True, "ok"
    if std < 7.0:
        return False, "flat"
    if std < 16.0 and sat < 14 and 30 < mean < 220:
        return False, "gray"
    return True, "ok"
