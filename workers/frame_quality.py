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
    A picture with a pure-green horizontal bar is decode concealment, not a thumb.
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
    buf, w, h, full_conceal = rgb
    # 细绿条缩成 96x54 会粘成一整片绿，所以先看原图
    if full_conceal:
        return False, "conceal"
    return _judge_rgb(buf, w, h)


def concealment_hwc(img):
    """画面还在，另外有一条纯绿横条。

    那是解码把丢失的切片涂绿，不是播出花屏。整幅都绿、看不出原来的画面时返回 False。
    必须用原图：缩成 96x54 或 224 之后，细绿条会粘成一整片绿。
    """
    try:
        import numpy as np
    except ImportError:
        return False
    arr = np.ascontiguousarray(img)
    if getattr(arr, "ndim", 0) != 3 or arr.shape[2] < 3:
        return False
    h, w = int(arr.shape[0]), int(arr.shape[1])
    if h < 8 or w < 8:
        return False
    rgb = arr[:, :, :3].astype(np.int16)
    r = rgb[:, :, 0]
    g = rgb[:, :, 1]
    b = rgb[:, :, 2]
    pure = (g > r + 40) & (g > b + 40) & (g > 70) & (r < 40) & (b < 40)
    pure_frac = pure.mean(axis=1)
    luma = (r * 3 + g * 6 + b) // 10
    std = luma.astype(np.float32).std(axis=1)
    fill = (pure_frac >= 0.70) & (std <= 28.0)
    picture = (pure_frac < 0.45) & (std >= 12.0)
    return _rows_are_concealment(fill.tolist(), picture.tolist())


def pure_green_ratio_hwc(img, block=8):
    """连成一块的纯绿占画面的比例。

    只数 8x8 里大部分像素都是纯绿、而且这一小块很平的地方。
    零散的绿点和树叶、绿衣服不算。不到一块的尺寸时退回整幅占比。
    """
    try:
        import numpy as np
    except ImportError:
        return 0.0
    arr = np.ascontiguousarray(img)
    if getattr(arr, "ndim", 0) != 3 or arr.shape[2] < 3:
        return 0.0
    h, w = int(arr.shape[0]), int(arr.shape[1])
    block = int(block)
    if h <= 0 or w <= 0 or block < 2:
        return 0.0
    rgb = arr[:, :, :3].astype(np.int16)
    r = rgb[:, :, 0]
    g = rgb[:, :, 1]
    b = rgb[:, :, 2]
    pure = (g > r + 40) & (g > b + 40) & (g > 70) & (r < 40) & (b < 40)
    if h < block or w < block:
        return float(pure.mean())
    bh, bw = h // block, w // block
    crop = pure[: bh * block, : bw * block]
    frac = crop.reshape(bh, block, bw, block).swapaxes(1, 2).mean(axis=(2, 3))
    luma = ((r * 3 + g * 6 + b) // 10).astype(np.float32)
    luma = luma[: bh * block, : bw * block]
    std = luma.reshape(bh, block, bw, block).swapaxes(1, 2).std(axis=(2, 3))
    hit = (frac >= 0.70) & (std <= 28.0)
    return float(hit.sum()) * (block * block) / float(h * w)


def pure_green_ratio_bytes(buf, w, h):
    w = int(w)
    h = int(h)
    n = w * h
    if w <= 0 or h <= 0 or not buf or len(buf) < n * 3:
        return 0.0
    try:
        import numpy as np

        arr = np.frombuffer(buf, dtype=np.uint8, count=n * 3)
        if arr.size == n * 3:
            return pure_green_ratio_hwc(arr.reshape((h, w, 3)))
    except Exception:
        pass
    return _pure_green_blocks_python(buf, w, h, 8)


def _pure_green_blocks_python(buf, w, h, block):
    block = int(block)
    if h < block or w < block:
        return 0.0
    mv = memoryview(buf)
    hit = 0
    by = 0
    while by + block <= h:
        bx = 0
        while bx + block <= w:
            pure = 0
            lum_sum = 0
            lum_sq = 0
            count = block * block
            yy = by
            while yy < by + block:
                xx = bx
                base = (yy * w + bx) * 3
                while xx < bx + block:
                    i = base + (xx - bx) * 3
                    r = mv[i]
                    g = mv[i + 1]
                    b = mv[i + 2]
                    if g > r + 40 and g > b + 40 and g > 70 and r < 40 and b < 40:
                        pure += 1
                    yv = (r * 3 + g * 6 + b) // 10
                    lum_sum += yv
                    lum_sq += yv * yv
                    xx += 1
                yy += 1
            frac = pure / float(count)
            mean = lum_sum / float(count)
            var = lum_sq / float(count) - mean * mean
            if var < 0:
                var = 0.0
            std = var ** 0.5
            if frac >= 0.70 and std <= 28.0:
                hit += 1
            bx += block
        by += block
    return hit * (block * block) / float(w * h)


def concealment_rgb_bytes(buf, w, h):
    """和 concealment_hwc 同一条规则，给没有数组的原始 RGB。"""
    w = int(w)
    h = int(h)
    n = w * h
    if w < 8 or h < 8 or not buf or len(buf) < n * 3:
        return False
    try:
        import numpy as np

        arr = np.frombuffer(buf, dtype=np.uint8, count=n * 3)
        if arr.size == n * 3:
            return concealment_hwc(arr.reshape((h, w, 3)))
    except Exception:
        pass
    return _concealment_python(buf, w, h)


def rgb_to_jpeg(rgb, width, height, quality=80):
    """RGB24 bytes -> JPEG bytes, or None."""
    if not rgb or width <= 0 or height <= 0:
        return None
    need = int(width) * int(height) * 3
    if len(rgb) < need:
        return None
    rgb = rgb[:need]
    try:
        import cv2
        import numpy as np

        img = np.frombuffer(rgb, dtype=np.uint8).reshape((height, width, 3))
        bgr = cv2.cvtColor(img, cv2.COLOR_RGB2BGR)
        ok, buf = cv2.imencode(
            ".jpg", bgr, [int(cv2.IMWRITE_JPEG_QUALITY), int(quality)]
        )
        if ok:
            return bytes(buf)
    except Exception:
        pass
    try:
        from PIL import Image
        import io

        im = Image.frombytes("RGB", (int(width), int(height)), rgb)
        buf = io.BytesIO()
        im.save(buf, format="JPEG", quality=int(quality))
        return buf.getvalue()
    except Exception:
        pass
    header = ("P6\n%d %d\n255\n" % (int(width), int(height))).encode("ascii")
    try:
        r = subprocess.run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "image2pipe",
                "-vcodec",
                "ppm",
                "-i",
                "pipe:0",
                "-frames:v",
                "1",
                "-q:v",
                "5",
                "-f",
                "image2",
                "pipe:1",
            ],
            input=header + rgb,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=3,
        )
        out = r.stdout or b""
        if out[:2] == b"\xff\xd8" and len(out) > 1024:
            return out
    except Exception:
        pass
    return None


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


def _rows_are_concealment(fill, picture):
    """纯绿横条占得够多，同时还有一块有细节的画面。"""
    h = len(fill)
    if h < 8 or len(picture) != h:
        return False
    fill_n = 0
    pic_n = 0
    best = 0
    cur = 0
    for i in range(h):
        if fill[i]:
            fill_n += 1
            cur += 1
            if cur > best:
                best = cur
        else:
            cur = 0
        if picture[i]:
            pic_n += 1
    if pic_n < h * 0.12:
        return False
    if fill_n < h * 0.12:
        return False
    if best < 4 or best < h * 0.04:
        return False
    return True


def _concealment_python(buf, w, h):
    step = 4 if w >= 320 else 1
    mv = memoryview(buf)
    fill = []
    picture = []
    for y in range(h):
        pure = 0
        n = 0
        lum_sum = 0
        lum_sq = 0
        base = y * w * 3
        x = 0
        while x < w:
            i = base + x * 3
            r = mv[i]
            g = mv[i + 1]
            b = mv[i + 2]
            n += 1
            if g > r + 40 and g > b + 40 and g > 70 and r < 40 and b < 40:
                pure += 1
            yv = (r * 3 + g * 6 + b) // 10
            lum_sum += yv
            lum_sq += yv * yv
            x += step
        if n <= 0:
            fill.append(False)
            picture.append(False)
            continue
        frac = pure / float(n)
        mean = lum_sum / float(n)
        var = lum_sq / float(n) - mean * mean
        if var < 0:
            var = 0.0
        std = var ** 0.5
        fill.append(frac >= 0.70 and std <= 28.0)
        picture.append(frac < 0.45 and std >= 12.0)
    return _rows_are_concealment(fill, picture)


def _decode_rgb_small(data):
    """返回 (小图 RGB, 宽, 高, 原图是不是解码绿条)。最后一项未知时是 None。"""
    try:
        import cv2
        import numpy as np

        arr = np.frombuffer(data, dtype=np.uint8)
        bgr = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if bgr is None:
            return None
        rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
        conceal = concealment_hwc(rgb)
        small = cv2.resize(rgb, (96, 54))
        return bytes(small.tobytes()), 96, 54, bool(conceal)
    except Exception:
        pass
    try:
        from PIL import Image
        import io

        im = Image.open(io.BytesIO(data)).convert("RGB")
        w, h = im.size
        conceal = False
        try:
            import numpy as np

            conceal = concealment_hwc(np.asarray(im))
        except Exception:
            conceal = concealment_rgb_bytes(im.tobytes(), w, h)
        im.thumbnail((96, 54))
        sw, sh = im.size
        return im.tobytes(), sw, sh, bool(conceal)
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
        return raw[:need], 96, 54, None
    except Exception:
        return None


def _judge_rgb(buf, w, h):
    n = int(w) * int(h)
    if n <= 0 or len(buf) < n * 3:
        return False, "bad_rgb"
    if concealment_rgb_bytes(buf, w, h):
        return False, "conceal"
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
