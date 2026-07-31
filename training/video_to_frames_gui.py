#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
视频抽帧 — 可视化界面（训练机用）

依赖：Python 自带 tkinter；抽帧逻辑同 video_to_frames.py（ffmpeg 或 OpenCV）

启动：
  cd training
  python video_to_frames_gui.py

Windows 也可：pythonw video_to_frames_gui.py
"""

from __future__ import annotations

import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from video_to_frames import run_extract, which_ffmpeg

# 常见视频后缀（实际能否解出取决于本机 ffmpeg/解码器）
VIDEO_TYPES = [
    ("视频文件", "*.mp4 *.ts *.mts *.m2ts *.mkv *.avi *.mov *.flv *.wmv *.webm *.mpg *.mpeg *.m4v *.mxf *.vob"),
    ("所有文件", "*.*"),
]


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("训练抽帧工具 — 视频 → 图片")
        self.geometry("640x520")
        self.minsize(560, 480)

        self.video_path = tk.StringVar()
        self.out_dir = tk.StringVar()
        self.mode = tk.StringVar(value="fps")  # fps | count
        self.fps = tk.StringVar(value="1")
        self.count = tk.StringVar(value="50")
        self.start = tk.StringVar(value="")
        self.end = tk.StringVar(value="")
        self.max_frames = tk.StringVar(value="")
        self.prefix = tk.StringVar(value="")
        self.unique_names = tk.BooleanVar(value=True)
        self.preset = tk.StringVar(value="自定义")
        self.busy = False

        self._build()
        self._log_ffmpeg_status()

    def _build(self):
        pad = {"padx": 10, "pady": 4}
        frm = ttk.Frame(self, padding=12)
        frm.pack(fill=tk.BOTH, expand=True)

        # 视频
        row = 0
        ttk.Label(frm, text="输入视频").grid(row=row, column=0, sticky="w", **pad)
        ttk.Entry(frm, textvariable=self.video_path, width=52).grid(
            row=row, column=1, sticky="ew", **pad
        )
        ttk.Button(frm, text="选择…", command=self.pick_video).grid(row=row, column=2, **pad)

        # 输出
        row += 1
        ttk.Label(frm, text="输出目录").grid(row=row, column=0, sticky="w", **pad)
        ttk.Entry(frm, textvariable=self.out_dir, width=52).grid(
            row=row, column=1, sticky="ew", **pad
        )
        ttk.Button(frm, text="选择…", command=self.pick_out).grid(row=row, column=2, **pad)

        # 快捷预设
        row += 1
        ttk.Label(frm, text="快捷输出").grid(row=row, column=0, sticky="w", **pad)
        pre = ttk.Frame(frm)
        pre.grid(row=row, column=1, columnspan=2, sticky="w", **pad)
        for label, sub in (
            ("→ train/anomaly", "train/anomaly"),
            ("→ train/normal", "train/normal"),
            ("→ val/anomaly", "val/anomaly"),
            ("→ val/normal", "val/normal"),
        ):
            ttk.Button(pre, text=label, command=lambda s=sub: self.preset_out(s)).pack(
                side=tk.LEFT, padx=2
            )

        # 模式
        row += 1
        ttk.Label(frm, text="抽帧方式").grid(row=row, column=0, sticky="w", **pad)
        mode_f = ttk.Frame(frm)
        mode_f.grid(row=row, column=1, columnspan=2, sticky="w", **pad)
        ttk.Radiobutton(
            mode_f, text="按每秒张数", variable=self.mode, value="fps"
        ).pack(side=tk.LEFT)
        ttk.Entry(mode_f, textvariable=self.fps, width=6).pack(side=tk.LEFT, padx=4)
        ttk.Label(mode_f, text="张/秒（0.5=每2秒1张）").pack(side=tk.LEFT)
        ttk.Radiobutton(
            mode_f, text="均匀固定张数", variable=self.mode, value="count"
        ).pack(side=tk.LEFT, padx=(16, 0))
        ttk.Entry(mode_f, textvariable=self.count, width=6).pack(side=tk.LEFT, padx=4)

        # 时间段
        row += 1
        ttk.Label(frm, text="时间段(秒)").grid(row=row, column=0, sticky="w", **pad)
        t = ttk.Frame(frm)
        t.grid(row=row, column=1, columnspan=2, sticky="w", **pad)
        ttk.Label(t, text="从").pack(side=tk.LEFT)
        ttk.Entry(t, textvariable=self.start, width=8).pack(side=tk.LEFT, padx=4)
        ttk.Label(t, text="到").pack(side=tk.LEFT)
        ttk.Entry(t, textvariable=self.end, width=8).pack(side=tk.LEFT, padx=4)
        ttk.Label(t, text="（可空=整段）").pack(side=tk.LEFT)

        # 上限 / 前缀
        row += 1
        ttk.Label(frm, text="最多张数").grid(row=row, column=0, sticky="w", **pad)
        m = ttk.Frame(frm)
        m.grid(row=row, column=1, columnspan=2, sticky="w", **pad)
        ttk.Entry(m, textvariable=self.max_frames, width=8).pack(side=tk.LEFT)
        ttk.Label(m, text="（可空不限制）  文件名前缀").pack(side=tk.LEFT, padx=(12, 4))
        ttk.Entry(m, textvariable=self.prefix, width=16).pack(side=tk.LEFT)
        ttk.Label(m, text="（可空=视频名）").pack(side=tk.LEFT)

        row += 1
        ttk.Checkbutton(
            frm,
            text="文件名带时间戳+随机串（推荐，避免覆盖以前抽的图）",
            variable=self.unique_names,
        ).grid(row=row, column=1, columnspan=2, sticky="w", **pad)

        # 说明
        row += 1
        tip = (
            "格式：常见 mp4/ts/mkv/avi/mov 等均可（由本机 ffmpeg 解码能力决定）。\n"
            "默认命名示例：fault_20260729_153045_a3f91c_00001.jpg，多次抽帧不会互相覆盖。\n"
            "抽帧不会自动判断异常，请抽完后人工筛选再训练。推荐安装 ffmpeg。"
        )
        ttk.Label(frm, text=tip, foreground="#444", justify=tk.LEFT).grid(
            row=row, column=0, columnspan=3, sticky="w", **pad
        )

        # 按钮
        row += 1
        btns = ttk.Frame(frm)
        btns.grid(row=row, column=0, columnspan=3, pady=10)
        self.btn_run = ttk.Button(btns, text="开始抽帧", command=self.on_run)
        self.btn_run.pack(side=tk.LEFT, padx=6)
        ttk.Button(btns, text="打开输出目录", command=self.open_out).pack(side=tk.LEFT, padx=6)

        # 日志
        row += 1
        ttk.Label(frm, text="运行日志").grid(row=row, column=0, sticky="nw", **pad)
        self.log = tk.Text(frm, height=12, width=70, state=tk.DISABLED)
        self.log.grid(row=row, column=1, columnspan=2, sticky="nsew", **pad)

        frm.columnconfigure(1, weight=1)
        frm.rowconfigure(row, weight=1)

        # 拖放（部分环境可用）
        try:
            self.drop_target_register("DND_Files")  # type: ignore
            self.dnd_bind("<<Drop>>", self._on_drop)  # type: ignore
        except Exception:
            pass

    def _log(self, msg: str):
        self.log.configure(state=tk.NORMAL)
        self.log.insert(tk.END, msg + "\n")
        self.log.see(tk.END)
        self.log.configure(state=tk.DISABLED)

    def _log_ffmpeg_status(self):
        if which_ffmpeg():
            self._log("已检测到 ffmpeg，将优先使用。")
        else:
            self._log("未检测到 ffmpeg，将尝试 OpenCV（请 pip install opencv-python-headless）。")

    def pick_video(self):
        p = filedialog.askopenfilename(title="选择视频", filetypes=VIDEO_TYPES)
        if p:
            self.video_path.set(p)
            if not self.prefix.get().strip():
                self.prefix.set(Path(p).stem)

    def pick_out(self):
        p = filedialog.askdirectory(title="选择输出目录")
        if p:
            self.out_dir.set(p)

    def preset_out(self, sub: str):
        """相对当前工作目录或已有 dataset 根。"""
        base = Path.cwd()
        # 若在 training/ 下运行，dataset 常在上一级
        for root in (base, base.parent, base / "dataset", base.parent / "dataset"):
            if (root / "train").is_dir() or root.name == "dataset":
                target = root / sub if root.name == "dataset" else root / "dataset" / sub
                break
        else:
            target = base.parent / "dataset" / sub
        target.mkdir(parents=True, exist_ok=True)
        self.out_dir.set(str(target.resolve()))
        self._log(f"输出目录已设为: {target}")

    def open_out(self):
        d = self.out_dir.get().strip()
        if not d:
            messagebox.showinfo("提示", "请先选择输出目录")
            return
        path = Path(d)
        path.mkdir(parents=True, exist_ok=True)
        import os
        import subprocess
        import sys

        if sys.platform.startswith("win"):
            os.startfile(path)  # type: ignore
        elif sys.platform == "darwin":
            subprocess.run(["open", str(path)])
        else:
            subprocess.run(["xdg-open", str(path)])

    def _parse_float(self, s: str, name: str) -> float | None:
        s = (s or "").strip()
        if not s:
            return None
        try:
            return float(s)
        except ValueError:
            raise ValueError(f"{name} 请填数字")

    def _parse_int(self, s: str, name: str) -> int | None:
        s = (s or "").strip()
        if not s:
            return None
        try:
            return int(s)
        except ValueError:
            raise ValueError(f"{name} 请填整数")

    def on_run(self):
        if self.busy:
            return
        video = self.video_path.get().strip()
        out = self.out_dir.get().strip()
        if not video:
            messagebox.showwarning("提示", "请选择输入视频")
            return
        if not out:
            messagebox.showwarning("提示", "请选择输出目录")
            return
        try:
            start = self._parse_float(self.start.get(), "开始时间")
            end = self._parse_float(self.end.get(), "结束时间")
            max_f = self._parse_int(self.max_frames.get(), "最多张数")
            if self.mode.get() == "count":
                count = self._parse_int(self.count.get(), "固定张数") or 50
                fps = 1.0
            else:
                count = None
                fps = self._parse_float(self.fps.get(), "每秒张数") or 1.0
        except ValueError as e:
            messagebox.showerror("参数错误", str(e))
            return

        pref = self.prefix.get().strip() or None
        self.busy = True
        self.btn_run.configure(state=tk.DISABLED)
        self._log("开始抽帧…")

        def work():
            err = None
            n = 0
            be = ""
            out_path = out
            try:
                n, be, out_p = run_extract(
                    video,
                    out,
                    fps=fps,
                    count=count,
                    start=start,
                    end=end,
                    prefix=pref,
                    max_frames=max_f,
                    backend="auto",
                    unique_names=self.unique_names.get(),
                )
                out_path = str(out_p)
            except Exception as e:
                err = e
            self.after(0, lambda: self._done(n, be, out_path, err))

        threading.Thread(target=work, daemon=True).start()

    def _done(self, n, be, out_path, err):
        self.busy = False
        self.btn_run.configure(state=tk.NORMAL)
        if err:
            self._log(f"失败: {err}")
            messagebox.showerror("抽帧失败", str(err))
            return
        self._log(f"完成：{n} 张（后端 {be}）→ {out_path}")
        self._log("请打开目录人工核对，再用于 train/val。")
        messagebox.showinfo("完成", f"共导出 {n} 张图片\n{out_path}")

    def _on_drop(self, event):
        # tkinterdnd2 风格；无扩展则不会绑上
        data = event.data
        if data:
            p = data.strip().strip("{}")
            if Path(p).is_file():
                self.video_path.set(p)


def main():
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()
