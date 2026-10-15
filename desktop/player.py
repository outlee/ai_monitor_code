# -*- coding: utf-8 -*-
"""本机只播一路。播放器优先用程序自带的 ffplay，其次用系统里的 ffplay 或 mpv。"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import List, Optional

_proc: Optional[subprocess.Popen] = None
_log_handle = None


def player_command(here: Path) -> Optional[List[str]]:
    bundled = [
        here / "player" / "ffplay.exe",
        here / "player" / "ffplay",
        here / "player" / "mpv.exe",
        here / "player" / "mpv",
    ]
    for path in bundled:
        if path.is_file():
            return [str(path)]
    for name in ("ffplay", "mpv"):
        found = shutil.which(name)
        if found:
            return [found]
    return None


def stop_player() -> None:
    global _proc, _log_handle
    proc = _proc
    _proc = None
    if proc is not None and proc.poll() is None:
        try:
            proc.terminate()
            proc.wait(timeout=2)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
    handle = _log_handle
    _log_handle = None
    if handle is not None:
        try:
            handle.close()
        except Exception:
            pass


def is_running() -> bool:
    proc = _proc
    return proc is not None and proc.poll() is None


def reap_if_dead() -> bool:
    """还在播返回 True。窗口已经关掉就收掉句柄并返回 False。"""
    proc = _proc
    if proc is None:
        return False
    if proc.poll() is None:
        return True
    stop_player()
    return False


def parse_mcast(url: str):
    """从 udp://@239.100.3.1:5000 取出地址和端口。不是组播地址就返回空。"""
    import re

    match = re.search(
        r"(?:udp://)?@?((?:22[4-9]|23[0-9])(?:\.\d{1,3}){3}):(\d{1,5})",
        str(url or ""),
        re.I,
    )
    if not match:
        return "", ""
    return match.group(1), match.group(2)


def _clean_env() -> dict:
    env = os.environ.copy()
    for key in list(env):
        if key.lower() in ("http_proxy", "https_proxy", "all_proxy"):
            env.pop(key, None)
    return env



def _preflight_http_preview(url: str, timeout: float = 4.0) -> Optional[str]:
    """HTTP preview: read a few TS packets first. Monitor may return 200 with 0 bytes."""
    if not str(url or "").lower().startswith(("http://", "https://")):
        return None
    try:
        from urllib.request import ProxyHandler, Request, build_opener
        req = Request(str(url), headers={"Accept": "*/*", "Connection": "close"})
        opener = build_opener(ProxyHandler({}))
        with opener.open(req, timeout=timeout) as resp:
            chunk = resp.read(188 * 14)
        if not chunk:
            return "监测预览接口无数据（HTTP 200 但 0 字节），频道收流可能正常但 /api/preview 未吐流。"
        return None
    except Exception as exc:
        msg = str(exc) or exc.__class__.__name__
        if "timed out" in msg.lower() or "timeout" in msg.lower():
            return "监测预览接口超时无数据（频道收流可能正常，但 /api/preview 未吐流）。"
        return "预览预检失败: %s" % msg


def _raise_window(title: str) -> None:
    """播放窗口默认在汇总窗口后面，点了像没反应。把它抬到最前。"""
    if sys.platform != "win32" or not title:
        return
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        found = []

        @ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
        def visit(hwnd, _lparam):
            length = user32.GetWindowTextLengthW(hwnd)
            if not length:
                return True
            buf = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buf, length + 1)
            if title in buf.value and user32.IsWindowVisible(hwnd):
                found.append(hwnd)
            return True

        for _ in range(20):
            found.clear()
            user32.EnumWindows(visit, 0)
            if found:
                user32.ShowWindow(found[0], 9)
                user32.SetForegroundWindow(found[0])
                return
            time.sleep(0.15)
    except Exception:
        pass


def start_player(here: Path, url: str, title: str) -> Optional[str]:
    """启动成功返回 None。找不到播放器或窗口马上退出时返回说明。"""
    global _proc, _log_handle
    cmd = player_command(here)
    if not cmd:
        return "没有找到播放器。把 ffplay 放在 desktop/player 里后再点。"
    stop_player()
    window_title = "预览 " + (title or "节目")
    name = Path(cmd[0]).name.lower()
    if "mpv" in name:
        args = cmd + [
            "--force-window=yes",
            "--ontop",
            "--title=" + window_title,
            "--geometry=960x540+80+80",
            url,
        ]
    else:
        args = cmd + [
            "-hide_banner",
            "-loglevel",
            "error",
            "-alwaysontop",
            "-window_title",
            window_title,
            "-left",
            "80",
            "-top",
            "80",
            "-x",
            "960",
            "-y",
            "540",
            "-rw_timeout",
            "5000000",
            url,
        ]
    log_path = here / "play.log"
    preflight = _preflight_http_preview(url)
    if preflight:
        try:
            log_path.write_text("play url=%s\npreflight: %s\n" % (url, preflight), encoding="utf-8")
        except OSError:
            pass
        return preflight
    handle = log_path.open("w", encoding="utf-8", errors="replace")
    try:
        handle.write("play url=%s\n" % url)
        handle.flush()
    except Exception:
        pass
    popen_kwargs = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": handle,
        "env": _clean_env(),
    }
    if sys.platform == "win32":
        popen_kwargs["creationflags"] = getattr(
            subprocess, "CREATE_NO_WINDOW", 0x08000000
        )
    try:
        proc = subprocess.Popen(
            args,
            **popen_kwargs,
        )
    except OSError as exc:
        handle.close()
        return "播放器没有启动: %s" % exc
    _proc = proc
    _log_handle = handle
    import threading

    threading.Thread(target=_raise_window, args=(window_title,), daemon=True).start()
    time.sleep(0.6)
    if proc.poll() is not None:
        try:
            handle.flush()
            detail = log_path.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            detail = ""
        stop_player()
        tail = detail[-240:] if detail else "没有留下原因"
        return "播放窗口马上关了。%s" % tail
    return None
