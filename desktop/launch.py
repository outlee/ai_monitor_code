# -*- coding: utf-8 -*-
"""在本机打开汇总窗口。只监听 127.0.0.1:8765。"""
from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path
from urllib.request import ProxyHandler, build_opener

def _frozen() -> bool:
    return bool(getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"))


def _bundle_root() -> Path:
    if _frozen():
        return Path(getattr(sys, "_MEIPASS"))
    return Path(__file__).resolve().parent.parent


def _writable_dir() -> Path:
    if _frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


BUNDLE_ROOT = _bundle_root()
WRITE_ROOT = _writable_dir()
if not _frozen():
    if str(BUNDLE_ROOT) not in sys.path:
        sys.path.insert(0, str(BUNDLE_ROOT))
    os.chdir(str(BUNDLE_ROOT))
else:
    os.chdir(str(WRITE_ROOT))

HOST = "127.0.0.1"
PORT = 8765
LOG_PATH = WRITE_ROOT / "launch.log"


def _log(text: str) -> None:
    line = time.strftime("%Y-%m-%d %H:%M:%S ") + text
    try:
        with LOG_PATH.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except OSError:
        pass
    # 双击 .py 时经常是 pythonw，标准输出是 None，print 会直接把程序打死。
    try:
        print(text, flush=True)
    except Exception:
        pass


def _box(text: str) -> None:
    """双击 py 时没有停住的黑窗口，用系统对话框把原因留下。"""
    if sys.platform != "win32":
        return
    try:
        import ctypes

        ctypes.windll.user32.MessageBoxW(None, text, "AI 节目监测", 0x10)
    except Exception:
        pass


def _wait_ready() -> bool:
    """只探本机接口。/api/health 要向三台监测机要库大小，半秒内回不来，会被误判成没启动。"""
    opener = build_opener(ProxyHandler({}))
    url = "http://%s:%d/api/desktop/info" % (HOST, PORT)
    for _ in range(50):
        try:
            with opener.open(url, timeout=1.0) as resp:
                if getattr(resp, "status", 200) == 200:
                    return True
        except Exception:
            time.sleep(0.1)
    return False


class DeskWindowApi:
    """页面拖分隔条时调用。左上角不动，只改外框宽高。"""

    def __init__(self) -> None:
        # 必须是下划线开头。pywebview 会把公开属性递归扫进页面接口，扫到窗口对象会在界面线程上死锁。
        self._window = None
        self._busy = threading.Lock()

    def resize_to(self, width, height):
        # 这条跑在 pywebview 的后台线程上。不要读窗口坐标，WinForms 的 Left/Top 只能在界面线程上读。
        win = self._window
        if win is None:
            return {"ok": False}
        try:
            w = int(float(width))
            h = int(float(height))
        except (TypeError, ValueError):
            return {"ok": False}
        w = max(480, min(8000, w))
        h = max(360, min(8000, h))
        if not self._busy.acquire(False):
            return {"ok": False, "busy": True}
        try:
            self._resize_on_ui(win, w, h)
        except Exception as exc:
            _log("调整窗口失败: %s" % exc)
            return {"ok": False, "error": str(exc)}
        finally:
            self._busy.release()
        return {"ok": True, "width": w, "height": h}

    def _resize_on_ui(self, win, width, height):
        """SetWindowPos 必须在界面线程上调，否则 WinForms 直接丢掉这次变长。"""
        form = None
        try:
            from webview.platforms.winforms import BrowserView

            form = BrowserView.instances.get(win.uid)
        except Exception:
            form = None
        if form is not None and form.InvokeRequired:
            from System import Func, Type

            def do():
                win.resize(width, height)

            form.Invoke(Func[Type](do))
            return
        win.resize(width, height)


def main() -> None:
    _log("开始启动，Python %s" % sys.executable)
    try:
        import uvicorn
    except ImportError:
        msg = (
            "缺少运行库，窗口起不来。\n请打开命令行，执行：\n"
            "py -3 -m pip install fastapi uvicorn python-multipart pywebview"
        )
        _log(msg)
        _box(msg)
        raise SystemExit(1)

    def serve() -> None:
        try:
            # Frozen: pass app object (module string path is unreliable under _MEIPASS).
            if _frozen():
                from desktop.server import app as desk_app

                uvicorn.run(
                    desk_app,
                    host=HOST,
                    port=PORT,
                    log_level="info",
                    access_log=False,
                    log_config=None,
                )
            else:
                uvicorn.run(
                    "desktop.server:app",
                    host=HOST,
                    port=PORT,
                    log_level="info",
                    access_log=False,
                    log_config=None,
                )
        except Exception as exc:
            _log("服务异常退出: %s" % exc)

    thread = threading.Thread(target=serve, name="desktop-http", daemon=True)
    thread.start()
    if not _wait_ready():
        msg = "本机汇总没有起来。端口 %d 可能被占用，或运行库不完整。详见 desktop\\launch.log" % PORT
        _log(msg)
        _box(msg)
        raise SystemExit(1)
    url = "http://%s:%d/" % (HOST, PORT)
    _log("服务已开: " + url)
    try:
        import webview

        api = DeskWindowApi()
        window = webview.create_window(
            "AI 节目监测", url, width=1440, height=900, js_api=api
        )
        api._window = window
        webview.start()
        return
    except Exception as exc:
        _log("独立窗口没打开，改用浏览器: %s" % exc)
    import webbrowser

    _log("用浏览器打开。播放器请放在 desktop\\player\\ffplay.exe")
    webbrowser.open(url)
    try:
        while thread.is_alive():
            time.sleep(1)
    except KeyboardInterrupt:
        _log("已退出")


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as exc:
        _log("启动失败: %s" % exc)
        _box("启动失败：%s\n详见 desktop\\launch.log" % exc)
        raise SystemExit(1)
