#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AI 节目监测 Worker
支持多路 UDP 组播（线程并行），FFmpeg 规则检测黑场/静帧/静音
可选 AI 模块（马赛克/花屏），默认关闭

P0/P1: 流断重连、事件 start/end、心跳、日志/截图轮转
P2: 检测前降采样、旁路 latest 帧（截图/AI 共用）、AI 独立线程
热重载: 监听 channels.yaml，进程内增删改监测线程

适配 CentOS + 纯 CPU 环境
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

import yaml

# 可选 AI 模块：导入失败也不影响主流程
try:
    from ai_detector import AIDetector, create_detector
except ImportError:
    try:
        from workers.ai_detector import AIDetector, create_detector
    except ImportError:
        AIDetector = None  # type: ignore
        create_detector = None  # type: ignore


# 全局事件文件写锁（多线程；进程间另用 fcntl）
_event_lock = threading.Lock()


def _now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _now_ts() -> float:
    return time.time()


class StreamMonitor:
    def __init__(
        self,
        channel: Dict,
        defaults: Dict,
        work_dir: str,
        ai_config: Optional[Dict] = None,
    ):
        self.channel = channel
        self.defaults = defaults
        self.work_dir = Path(work_dir)
        self.id = channel["id"]
        self.name = channel.get("name", self.id)
        self.url = channel["url"]
        self.enabled = channel.get("enabled", True)
        # MPEG-TS program id（可选）。SPTS 多数不填；MPTS 填 service/program 号
        self.program = self._parse_program(channel.get("program"))

        self.black_duration = float(
            channel.get("black_duration", defaults.get("black_duration", 3.0))
        )
        self.freeze_duration = float(
            channel.get("freeze_duration", defaults.get("freeze_duration", 5.0))
        )
        self.silence_duration = float(
            channel.get("silence_duration", defaults.get("silence_duration", 5.0))
        )
        self.silence_threshold = channel.get(
            "silence_threshold", defaults.get("silence_threshold", -40)
        )
        self.save_snapshot = channel.get(
            "save_snapshot", defaults.get("save_snapshot", True)
        )

        # 运维参数 (P0/P1)
        self.reconnect_delay = float(defaults.get("reconnect_delay", 5.0))
        self.reconnect_max_delay = float(defaults.get("reconnect_max_delay", 60.0))
        self.heartbeat_interval = float(defaults.get("heartbeat_interval", 5.0))
        self.events_max_bytes = int(defaults.get("events_max_bytes", 50 * 1024 * 1024))
        self.events_keep_files = int(defaults.get("events_keep_files", 5))
        self.snapshot_max_per_channel = int(
            defaults.get("snapshot_max_per_channel", 100)
        )

        # 性能参数 (P2)
        # detect_width: 规则检测最大宽度，0=不缩放；推荐 320~640
        self.detect_width = int(
            channel.get("detect_width", defaults.get("detect_width", 480))
        )
        # 旁路最新帧刷新间隔（秒）；0=关闭旁路（截图/AI 退回独立 FFmpeg）
        self.frame_interval_sec = float(
            channel.get(
                "frame_interval_sec", defaults.get("frame_interval_sec", 2.0)
            )
        )
        # 旁路帧用于截图的最大新鲜度（秒）
        self.latest_max_age_sec = float(defaults.get("latest_max_age_sec", 5.0))
        # 告警截图是否优先用 latest（避免再拉一路）
        self.snapshot_prefer_latest = bool(
            defaults.get("snapshot_prefer_latest", True)
        )
        # AI 是否走独立线程
        self.ai_async = bool(defaults.get("ai_async", True))

        snap_root = defaults.get("snapshot_dir", "snapshots")
        log_root = defaults.get("log_dir", "logs")
        self.snapshot_dir = self.work_dir / snap_root / self.id
        self.log_dir = self.work_dir / log_root
        self.status_dir = self.log_dir / "status"
        self.snapshot_dir.mkdir(parents=True, exist_ok=True)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.status_dir.mkdir(parents=True, exist_ok=True)

        # 旁路共享帧（主 FFmpeg 持续覆盖写）
        self.latest_frame_path = self.snapshot_dir / "latest.jpg"
        # 仅串行化本进程内读/拷路径；FFmpeg 写 latest 不持此锁，靠 JPEG 完整性重试防半帧
        self._latest_lock = threading.Lock()

        self.process: Optional[subprocess.Popen] = None
        self.running = False
        self._thread: Optional[threading.Thread] = None
        self._ai_thread: Optional[threading.Thread] = None
        self.reconnect_count = 0
        self._last_heartbeat_ts = 0.0
        self._last_ffmpeg_activity_ts = 0.0
        self._state = "init"
        self._active_alarms: Dict[str, float] = {}  # type -> start_ts
        self._status_lock = threading.Lock()
        self._snapshot_inflight = False
        self._snapshot_lock = threading.Lock()

        self.logger = logging.getLogger(f"monitor.{self.id}")
        self._setup_logger()

        # AI 检测器（可选）
        self.ai: Optional[Any] = None
        self._last_ai_ts = 0.0
        self._init_ai(ai_config or {})

    @staticmethod
    def _parse_program(raw: Any) -> Optional[int]:
        if raw is None:
            return None
        if isinstance(raw, str) and not raw.strip():
            return None
        try:
            return int(raw)
        except (TypeError, ValueError):
            return None

    def _v_label(self) -> str:
        """filter_complex 视频输入标签。"""
        if self.program is not None:
            return f"0:p:{self.program}:v"
        return "0:v"

    def _a_label(self) -> str:
        """filter_complex 音频输入标签。"""
        if self.program is not None:
            return f"0:p:{self.program}:a"
        return "0:a"

    def _setup_logger(self):
        log_file = self.log_dir / f"{self.id}.log"
        handler = logging.FileHandler(log_file, encoding="utf-8")
        formatter = logging.Formatter(
            "%(asctime)s [%(levelname)s] %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
        handler.setFormatter(formatter)
        if not self.logger.handlers:
            self.logger.addHandler(handler)
            console = logging.StreamHandler()
            console.setFormatter(formatter)
            self.logger.addHandler(console)
        self.logger.setLevel(logging.INFO)
        self.logger.propagate = False

    def _init_ai(self, ai_config: Dict):
        if create_detector is None:
            self.logger.info("AI 模块代码未找到，仅使用规则检测")
            return
        try:
            self.ai = create_detector({"ai": ai_config}, str(self.work_dir))
            st = self.ai.status()
            self.logger.info(
                f"AI 状态: enabled={st['enabled']}, available={st['available']}, "
                f"backend={st['backend']}, async={self.ai_async}"
            )
        except Exception as e:
            self.logger.warning(f"AI 初始化失败（不影响规则检测）: {e}")
            self.ai = None

    def _input_url_with_timeout(self) -> str:
        """
        为 UDP/RTP 注入收包超时，避免无流时 FFmpeg 永久阻塞、无法进入重连。
        超时单位：微秒（FFmpeg udp 协议 timeout 选项）。
        """
        url = self.url
        if "timeout=" in url:
            return url
        lower = url.lower()
        if not (lower.startswith("udp:") or lower.startswith("rtp:")):
            return url
        timeout_us = int(float(self.defaults.get("input_timeout_sec", 15.0)) * 1_000_000)
        sep = "&" if "?" in url else "?"
        return f"{url}{sep}timeout={timeout_us}"

    def _build_filter_complex(self) -> str:
        """
        视频：可选 program 选轨 → 降采样 → split → 规则检测 + 旁路 fps
        音频：silencedetect（同样按 program 选轨）
        """
        vin = self._v_label()
        ain = self._a_label()
        detect = (
            f"blackdetect=d={self.black_duration}:pix_th=0.10,"
            f"freezedetect=n=0.003:d={self.freeze_duration}"
        )
        audio = (
            f"[{ain}]silencedetect=noise={self.silence_threshold}dB:"
            f"d={self.silence_duration}[aout]"
        )

        use_side = self.frame_interval_sec > 0
        dw = self.detect_width

        if use_side:
            fps = max(self.frame_interval_sec, 0.2)
            if dw and dw > 0:
                v = (
                    f"[{vin}]scale=w='min(iw\\,{dw})':h=-2:flags=fast_bilinear[vs];"
                    f"[vs]split=2[vd][vf];"
                    f"[vd]{detect}[vout];"
                    f"[vf]fps=1/{fps},"
                    f"scale=w='min(iw\\,640)':h=-2:flags=fast_bilinear[vsnap]"
                )
            else:
                v = (
                    f"[{vin}]split=2[vd][vf];"
                    f"[vd]{detect}[vout];"
                    f"[vf]fps=1/{fps},"
                    f"scale=w='min(iw\\,640)':h=-2:flags=fast_bilinear[vsnap]"
                )
            return f"{v};{audio}"

        if dw and dw > 0:
            v = (
                f"[{vin}]scale=w='min(iw\\,{dw})':h=-2:flags=fast_bilinear,"
                f"{detect}[vout]"
            )
        else:
            v = f"[{vin}]{detect}[vout]"
        return f"{v};{audio}"

    def _build_ffmpeg_cmd(self) -> List[str]:
        """单进程：规则检测 + 可选旁路 latest.jpg；MPTS 用 program 选节目。"""
        fc = self._build_filter_complex()
        cmd: List[str] = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "level+info",
            "-fflags",
            "+genpts",
            "-rw_timeout",
            "15000000",
        ]
        # MPTS 选 program 时加大探测，便于解析 PAT/PMT
        if self.program is not None:
            cmd.extend(
                [
                    "-probesize",
                    "32M",
                    "-analyzeduration",
                    "10M",
                ]
            )
        cmd.extend(
            [
                "-i",
                self._input_url_with_timeout(),
                "-filter_complex",
                fc,
                "-map",
                "[vout]",
                "-map",
                "[aout]",
                "-f",
                "null",
                "-",
            ]
        )
        if self.frame_interval_sec > 0:
            cmd.extend(
                [
                    "-map",
                    "[vsnap]",
                    "-vsync",
                    "0",
                    "-q:v",
                    "4",
                    "-update",
                    "1",
                    "-y",
                    str(self.latest_frame_path),
                ]
            )
        return cmd

    # ---------- 心跳状态 ----------

    def _write_status(self, state: Optional[str] = None, extra: Optional[Dict] = None):
        if state is not None:
            self._state = state
        latest_age = self._latest_frame_age()
        payload = {
            "channel_id": self.id,
            "channel_name": self.name,
            "url": self.url,
            "program": self.program,
            "enabled": self.enabled,
            "state": self._state,
            "ffmpeg_pid": self.process.pid
            if self.process and self.process.poll() is None
            else None,
            "worker_pid": os.getpid(),
            "last_heartbeat": _now_str(),
            "last_heartbeat_ts": _now_ts(),
            "last_ffmpeg_activity_ts": self._last_ffmpeg_activity_ts or None,
            "reconnect_count": self.reconnect_count,
            "active_alarms": list(self._active_alarms.keys()),
            "detect_width": self.detect_width,
            "frame_interval_sec": self.frame_interval_sec,
            "latest_frame_age_sec": round(latest_age, 2)
            if latest_age is not None
            else None,
            "ai_async": self.ai_async,
        }
        if extra:
            payload.update(extra)
        path = self.status_dir / f"{self.id}.json"
        tmp = path.with_suffix(".json.tmp")
        try:
            with self._status_lock:
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(payload, f, ensure_ascii=False)
                tmp.replace(path)
            self._last_heartbeat_ts = payload["last_heartbeat_ts"]
        except OSError as e:
            self.logger.debug(f"写心跳失败: {e}")

    def _maybe_heartbeat(self):
        if _now_ts() - self._last_heartbeat_ts >= self.heartbeat_interval:
            self._write_status()

    # ---------- 事件与轮转 ----------

    def _rotate_events_if_needed(self, event_file: Path):
        """events.jsonl 超过阈值时轮转为 events.jsonl.1 .. .N"""
        try:
            if not event_file.is_file():
                return
            if event_file.stat().st_size < self.events_max_bytes:
                return
            base = event_file
            oldest = Path(str(base) + f".{self.events_keep_files}")
            if oldest.is_file():
                oldest.unlink(missing_ok=True)
            for i in range(self.events_keep_files - 1, 0, -1):
                src = Path(str(base) + f".{i}")
                dst = Path(str(base) + f".{i + 1}")
                if src.is_file():
                    src.replace(dst)
            base.replace(Path(str(base) + ".1"))
            self.logger.info(
                f"事件日志已轮转: {base.name} -> {base.name}.1 "
                f"(阈值 {self.events_max_bytes} 字节)"
            )
        except OSError as e:
            self.logger.warning(f"事件日志轮转失败: {e}")

    def _save_event(self, event: Dict):
        """多线程 + 多进程安全追加 events.jsonl。

        - 进程内: threading.Lock
        - 进程间: fcntl.flock(.events.lock)
        - 轮转与写入在同一把文件锁内完成，避免交错
        """
        event_file = self.log_dir / "events.jsonl"
        lock_file = self.log_dir / ".events.lock"
        line = json.dumps(event, ensure_ascii=False) + "\n"
        try:
            with _event_lock:
                self.log_dir.mkdir(parents=True, exist_ok=True)
                with open(lock_file, "a+", encoding="utf-8") as lf:
                    fcntl.flock(lf.fileno(), fcntl.LOCK_EX)
                    try:
                        self._rotate_events_if_needed(event_file)
                        with open(event_file, "a", encoding="utf-8") as f:
                            f.write(line)
                            f.flush()
                            try:
                                os.fsync(f.fileno())
                            except OSError:
                                pass
                    finally:
                        fcntl.flock(lf.fileno(), fcntl.LOCK_UN)
        except OSError as e:
            self.logger.error(f"写事件失败: {e}")

    def _prune_snapshots(self):
        """保留每个频道最近 N 张 jpg（不删 latest / 点文件 / AI 临时读文件）。"""
        try:
            files = sorted(
                (
                    p
                    for p in self.snapshot_dir.glob("*.jpg")
                    if p.name != "latest.jpg"
                    and not p.name.startswith(".")
                    and not p.name.startswith(".ai_read_")
                    and not p.name.endswith(".part")
                ),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
            for old in files[self.snapshot_max_per_channel :]:
                try:
                    old.unlink(missing_ok=True)
                except OSError:
                    pass
        except OSError as e:
            self.logger.debug(f"截图清理失败: {e}")

    # ---------- 共享帧 / 截图 ----------

    def _latest_frame_age(self) -> Optional[float]:
        try:
            if not self.latest_frame_path.is_file():
                return None
            return _now_ts() - self.latest_frame_path.stat().st_mtime
        except OSError:
            return None

    @staticmethod
    def _is_complete_jpeg(data: bytes) -> bool:
        """粗检 JPEG 是否完整（SOI…EOI），降低读到半帧的概率。"""
        if data is None or len(data) < 128:
            return False
        # SOI
        if data[0] != 0xFF or data[1] != 0xD8:
            return False
        # EOI：找最后一个 FFD9，且须在 SOI 之后（兼容末尾少量 padding）
        eoi = data.rfind(b"\xff\xd9")
        if eoi < 2:
            return False
        return True

    def _read_latest_jpeg_bytes(self, max_retries: int = 4) -> Optional[bytes]:
        """在 FFmpeg 可能正在覆盖写 latest.jpg 时，尽量读到完整 JPEG。

        写端无法加锁（FFmpeg -update 1），故读端重试 + 完整性检查。
        """
        for attempt in range(max_retries):
            try:
                with self._latest_lock:
                    if not self.latest_frame_path.is_file():
                        return None
                    age = _now_ts() - self.latest_frame_path.stat().st_mtime
                    if age > self.latest_max_age_sec:
                        return None
                    data = self.latest_frame_path.read_bytes()
                if self._is_complete_jpeg(data):
                    return data
            except OSError:
                pass
            time.sleep(0.03 * (attempt + 1))
        return None

    def _copy_latest_frame(self, dest: Path) -> bool:
        """从旁路 latest.jpg 安全复制到 dest（完整 JPEG 才写入）。"""
        data = self._read_latest_jpeg_bytes()
        if not data:
            return False
        tmp = dest.with_suffix(dest.suffix + ".part")
        try:
            with self._latest_lock:
                tmp.write_bytes(data)
                tmp.replace(dest)
            return dest.is_file() and dest.stat().st_size > 0
        except OSError as e:
            self.logger.debug(f"复制 latest 失败: {e}")
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            return False

    def _grab_frame_ffmpeg(self, out_path: Path, quality: int = 3) -> bool:
        """独立 FFmpeg 抽 1 帧（回退路径，会多占一路连接）。"""
        try:
            src = (
                self._input_url_with_timeout()
                if self.url.lower().startswith(("udp:", "rtp:"))
                else self.url
            )
            cmd = [
                "ffmpeg",
                "-y",
                "-hide_banner",
                "-loglevel",
                "error",
                "-rw_timeout",
                "8000000",
            ]
            if self.program is not None:
                cmd.extend(["-probesize", "32M", "-analyzeduration", "10M"])
            cmd.extend(["-i", src])
            if self.program is not None:
                cmd.extend(["-map", f"0:p:{self.program}:v"])
            cmd.extend(
                [
                    "-frames:v",
                    "1",
                    "-q:v",
                    str(quality),
                    str(out_path),
                ]
            )
            subprocess.run(
                cmd,
                timeout=10,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return out_path.is_file() and out_path.stat().st_size > 0
        except Exception as e:
            self.logger.debug(f"独立抽帧失败: {e}")
            return False

    def _take_snapshot(self, event_type: str) -> Optional[Path]:
        """
        优先复制旁路 latest.jpg；过期或不存在时再独立拉帧。
        复制路径很快，不阻塞；独立拉帧放到后台线程，避免堵 stderr。
        """
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        out_path = self.snapshot_dir / f"{event_type}_{ts}.jpg"

        if self.snapshot_prefer_latest and self._copy_latest_frame(out_path):
            self.logger.info(f"截图已保存(旁路): {out_path}")
            self._prune_snapshots()
            return out_path

        # 回退：后台独立 FFmpeg，避免阻塞检测循环
        def _bg():
            with self._snapshot_lock:
                if self._snapshot_inflight:
                    return
                self._snapshot_inflight = True
            try:
                if self._grab_frame_ffmpeg(out_path, quality=3):
                    self.logger.info(f"截图已保存(独立拉流): {out_path}")
                    self._prune_snapshots()
                else:
                    self.logger.warning(f"截图失败: {out_path}")
            finally:
                with self._snapshot_lock:
                    self._snapshot_inflight = False

        threading.Thread(
            target=_bg, name=f"snap-{self.id}-{event_type}", daemon=True
        ).start()
        return None

    # ---------- FFmpeg 行解析 ----------

    _RE_DURATION = re.compile(
        r"(?:black_duration|freeze_duration|silence_duration)\s*[:=]\s*([0-9.]+)",
        re.I,
    )

    def _emit_alarm_event(
        self,
        *,
        alarm_key: str,
        is_start: bool,
        is_end: bool,
        event: Dict,
    ):
        if is_start and alarm_key:
            self._active_alarms[alarm_key] = _now_ts()
        if is_end and alarm_key:
            start_ts = self._active_alarms.pop(alarm_key, None)
            if start_ts and "duration" not in event:
                event["duration"] = round(_now_ts() - start_ts, 3)

        level = self.logger.warning if is_start else self.logger.info
        level(json.dumps(event, ensure_ascii=False))
        self._save_event(event)

        if is_start and self.save_snapshot:
            self._take_snapshot(event["type"])

        self._write_status()

    def _parse_ffmpeg_line(self, line: str):
        """
        解析检测日志。同一行可能同时含 start+end（如 blackdetect 汇总行），
        按 start 再 end 顺序各发一条事件。
        """
        line = line.strip()
        if not line:
            return

        self._last_ffmpeg_activity_ts = _now_ts()
        now = _now_str()
        lower = line.lower()

        # 按类型检查；同一行可先后发出 start 与 end
        pairs = (
            ("black", "black_start", "black_end", "黑场"),
            ("freeze", "freeze_start", "freeze_end", "静帧"),
            ("silence", "silence_start", "silence_end", "静音"),
        )
        handled = False
        for key, start_tok, end_tok, label in pairs:
            has_start = start_tok in lower
            has_end = end_tok in lower
            if not has_start and not has_end:
                continue
            handled = True
            if has_start:
                self._emit_alarm_event(
                    alarm_key=key,
                    is_start=True,
                    is_end=False,
                    event={
                        "type": key,
                        "phase": "start",
                        "channel_id": self.id,
                        "channel_name": self.name,
                        "message": f"检测到{label}开始: {line}",
                        "time": now,
                    },
                )
            if has_end:
                dur = self._extract_duration(line)
                ev = {
                    "type": f"{key}_end",
                    "phase": "end",
                    "channel_id": self.id,
                    "channel_name": self.name,
                    "message": f"{label}结束: {line}",
                    "time": now,
                }
                if dur is not None:
                    ev["duration"] = dur
                self._emit_alarm_event(
                    alarm_key=key,
                    is_start=False,
                    is_end=True,
                    event=ev,
                )
        if not handled:
            return

    def _extract_duration(self, line: str) -> Optional[float]:
        m = self._RE_DURATION.search(line)
        if m:
            try:
                return float(m.group(1))
            except ValueError:
                return None
        return None

    # ---------- AI（旁路线程，不堵 stderr） ----------

    def _analyze_frame_for_ai(self, frame_path: Path) -> None:
        if not self.ai or not self.ai.is_ready:
            return
        # 若源是 FFmpeg 正在写的 latest.jpg，先拷到私有文件再推理，避免半帧
        work_path = frame_path
        tmp_copy: Optional[Path] = None
        try:
            if frame_path.resolve() == self.latest_frame_path.resolve():
                tmp_copy = self.snapshot_dir / f".ai_read_{os.getpid()}_{threading.get_ident()}.jpg"
                if not self._copy_latest_frame(tmp_copy):
                    return
                work_path = tmp_copy
            result = self.ai.analyze_image(str(work_path))
            if not result.get("is_anomaly"):
                return
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            label = result.get("label", "anomaly")
            archive = self.snapshot_dir / f"ai_{label}_{ts}.jpg"
            try:
                if work_path.is_file():
                    shutil.copy2(work_path, archive)
                else:
                    archive = work_path
            except OSError:
                archive = work_path
            event = {
                "type": "ai_" + label,
                "phase": "start",
                "channel_id": self.id,
                "channel_name": self.name,
                "message": result.get("message", "AI 检测到画面异常"),
                "score": result.get("score"),
                "detail": result.get("detail"),
                "backend": result.get("backend"),
                "time": _now_str(),
                "snapshot": str(archive),
            }
            self.logger.warning(json.dumps(event, ensure_ascii=False))
            self._save_event(event)
            self._prune_snapshots()
        except Exception as e:
            self.logger.debug(f"AI 分析跳过: {e}")
        finally:
            if tmp_copy is not None:
                try:
                    tmp_copy.unlink(missing_ok=True)
                except OSError:
                    pass

    def _ai_loop(self):
        """独立线程：按 interval 读 latest 或回退抽帧，不阻塞规则解析。"""
        self.logger.info("AI 旁路线程已启动")
        while self.running:
            try:
                if not self.ai or not self.ai.is_ready:
                    time.sleep(1.0)
                    continue
                if self._state not in ("running", "starting"):
                    time.sleep(0.5)
                    continue

                interval = float(getattr(self.ai, "interval_sec", 2.0) or 2.0)
                now = time.time()
                if now - self._last_ai_ts < interval:
                    time.sleep(0.2)
                    continue

                used = False
                # 优先旁路帧
                age = self._latest_frame_age()
                if age is not None and age <= max(self.latest_max_age_sec, interval * 2):
                    self._last_ai_ts = now
                    self._analyze_frame_for_ai(self.latest_frame_path)
                    used = True
                else:
                    # 旁路未就绪：低频独立抽帧（仍在 AI 线程，不堵主循环）
                    tmp = self.snapshot_dir / "ai_frame_tmp.jpg"
                    if self._grab_frame_ffmpeg(tmp, quality=4):
                        self._last_ai_ts = now
                        self._analyze_frame_for_ai(tmp)
                        try:
                            if tmp.is_file():
                                tmp.unlink(missing_ok=True)
                        except OSError:
                            pass
                        used = True

                if not used:
                    time.sleep(0.5)
                else:
                    time.sleep(0.1)
            except Exception as e:
                self.logger.debug(f"AI 线程异常: {e}")
                time.sleep(1.0)
        self.logger.info("AI 旁路线程已退出")

    def _start_ai_thread(self):
        if not self.ai or not self.ai.is_ready:
            return
        if not self.ai_async:
            return
        if self._ai_thread and self._ai_thread.is_alive():
            return
        self._ai_thread = threading.Thread(
            target=self._ai_loop,
            name=f"ai-{self.id}",
            daemon=True,
        )
        self._ai_thread.start()

    def _maybe_run_ai_inline(self):
        """ai_async=false 时在主循环低频触发（兼容旧行为，仍尽量用 latest）。"""
        if self.ai_async:
            return
        if not self.ai or not self.ai.is_ready:
            return
        now = time.time()
        interval = float(getattr(self.ai, "interval_sec", 2.0) or 2.0)
        if now - self._last_ai_ts < interval:
            return
        age = self._latest_frame_age()
        if age is not None and age <= max(self.latest_max_age_sec, interval * 2):
            self._last_ai_ts = now
            self._analyze_frame_for_ai(self.latest_frame_path)
            return
        # 回退独立抽帧也放到短线程，避免长时间阻塞
        self._last_ai_ts = now

        def _bg():
            tmp = self.snapshot_dir / "ai_frame_tmp.jpg"
            if self._grab_frame_ffmpeg(tmp, quality=4):
                self._analyze_frame_for_ai(tmp)
                try:
                    tmp.unlink(missing_ok=True)
                except OSError:
                    pass

        threading.Thread(target=_bg, name=f"ai-inline-{self.id}", daemon=True).start()

    # ---------- 运行 / 重连 ----------

    def _interruptible_sleep(self, seconds: float):
        end = time.time() + seconds
        while self.running and time.time() < end:
            time.sleep(min(0.5, end - time.time()))

    def _stop_ffmpeg(self):
        if self.process and self.process.poll() is None:
            try:
                self.process.terminate()
                try:
                    self.process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(timeout=3)
            except Exception as e:
                self.logger.debug(f"停止 FFmpeg: {e}")
        self.process = None

    def _run_once(self) -> int:
        """跑一轮 FFmpeg，返回退出码。主循环只解析 stderr + 心跳。"""
        cmd = self._build_ffmpeg_cmd()
        prog = f" program={self.program}" if self.program is not None else ""
        self.logger.info(
            f"启动 FFmpeg 监测: {self.name} ({self.url}){prog} "
            f"detect_width={self.detect_width} "
            f"frame_interval={self.frame_interval_sec}s"
        )
        self.logger.debug("FFmpeg cmd: %s", " ".join(cmd))
        self.process = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            universal_newlines=True,
            bufsize=1,
        )
        self._write_status("running")
        self._start_ai_thread()

        assert self.process.stderr is not None
        while self.running and self.process.poll() is None:
            line = self.process.stderr.readline()
            if line:
                self._parse_ffmpeg_line(line)
            self._maybe_run_ai_inline()
            self._maybe_heartbeat()

        if self.process.stderr:
            try:
                for line in self.process.stderr:
                    if not self.running:
                        break
                    if line:
                        self._parse_ffmpeg_line(line)
            except Exception:
                pass

        rc = self.process.returncode if self.process else -1
        self.process = None
        return rc if rc is not None else -1

    def run(self):
        """主循环：监测 + 断流重连（在独立线程中调用）。"""
        if not self.enabled:
            self.logger.info(f"频道 {self.name} 已禁用，跳过")
            self._write_status("disabled")
            return

        self.running = True
        delay = self.reconnect_delay
        self.logger.info(f"监测线程启动: {self.name} ({self.url})")
        self._start_ai_thread()

        while self.running:
            try:
                rc = self._run_once()
            except Exception as e:
                self.logger.error(f"监测异常: {e}")
                rc = -1

            if not self.running:
                break

            if rc in (-15, -9):
                break

            self.reconnect_count += 1
            self._active_alarms.clear()
            self._write_status("reconnecting")

            event = {
                "type": "stream_down",
                "phase": "start",
                "channel_id": self.id,
                "channel_name": self.name,
                "message": (
                    f"流中断或 FFmpeg 退出 (code={rc})，"
                    f"{delay:.1f}s 后第 {self.reconnect_count} 次重连"
                ),
                "returncode": rc,
                "reconnect_count": self.reconnect_count,
                "time": _now_str(),
            }
            self.logger.warning(json.dumps(event, ensure_ascii=False))
            self._save_event(event)

            self._interruptible_sleep(delay)
            delay = min(delay * 1.5, self.reconnect_max_delay)

        self._stop_ffmpeg()
        self._write_status("stopped")
        self.logger.info(f"监测线程结束: {self.name}")

    def start_thread(self) -> threading.Thread:
        t = threading.Thread(
            target=self.run,
            name=f"monitor-{self.id}",
            daemon=True,
        )
        self._thread = t
        t.start()
        return t

    def stop(self):
        self.running = False
        self._stop_ffmpeg()
        self._write_status("stopped")


def load_config(config_path: str) -> Dict:
    with open(config_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def channel_runtime_fingerprint(
    channel: Dict, defaults: Dict, ai_config: Dict
) -> str:
    """单路监测指纹：变则需停旧线程、起新线程。"""
    payload = {
        "channel": channel,
        "defaults": defaults or {},
        "ai": ai_config or {},
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


class WorkerRuntime:
    """
    管理本进程内多路 StreamMonitor 线程，并支持配置热重载。

    - 若启动时指定 --ids：只管理这些 ID（Manager 分组用）；
      其中 enabled=false 或已删除的会停掉；参数变更会重启该路。
    - 未指定 --ids：管理配置里全部 enabled 频道（单机直跑）。
    """

    def __init__(
        self,
        config_path: str,
        work_dir: str,
        id_filter: Optional[List[str]] = None,
        reload_interval: float = 3.0,
        enable_reload: bool = True,
    ):
        self.config_path = Path(config_path).resolve()
        self.work_dir = Path(work_dir).resolve()
        self.id_filter: Optional[Set[str]] = set(id_filter) if id_filter else None
        self.reload_interval = max(1.0, float(reload_interval))
        self.enable_reload = enable_reload
        self.running = True

        self._lock = threading.Lock()
        self.monitors: Dict[str, StreamMonitor] = {}
        self.threads: Dict[str, threading.Thread] = {}
        self.fingerprints: Dict[str, str] = {}
        self._config_mtime: float = 0.0
        self._reload_count = 0

    def _select_channels(self, config: Dict) -> List[Dict]:
        channels = list(config.get("channels") or [])
        if self.id_filter is not None:
            # 固定 ID 集合：仅处理 filter 内且仍 enabled 的
            out = []
            for ch in channels:
                cid = ch.get("id")
                if cid not in self.id_filter:
                    continue
                if not ch.get("enabled", True):
                    continue
                out.append(ch)
            return out
        return [ch for ch in channels if ch.get("enabled", True)]

    def reconcile(self, reason: str = "") -> None:
        try:
            config = load_config(str(self.config_path))
        except Exception as e:
            logging.getLogger("worker").error(f"读配置失败: {e}")
            return

        defaults = config.get("defaults") or {}
        ai_config = config.get("ai") or {}
        desired = self._select_channels(config)
        desired_map = {ch["id"]: ch for ch in desired if ch.get("id")}
        desired_fps = {
            cid: channel_runtime_fingerprint(ch, defaults, ai_config)
            for cid, ch in desired_map.items()
        }

        with self._lock:
            current_ids = set(self.monitors.keys())
            want_ids = set(desired_map.keys())

            # 停止多余
            for cid in sorted(current_ids - want_ids):
                self._stop_one(cid, reason=reason or "配置移除/禁用")

            # 新增或变更
            started = restarted = kept = 0
            for cid in sorted(want_ids):
                fp = desired_fps[cid]
                if cid not in self.monitors:
                    self._start_one(desired_map[cid], defaults, ai_config, fp)
                    started += 1
                    continue
                # 线程已死：拉起
                t = self.threads.get(cid)
                if t is None or not t.is_alive():
                    self._stop_one(cid, reason="线程已退出")
                    self._start_one(desired_map[cid], defaults, ai_config, fp)
                    restarted += 1
                    continue
                if self.fingerprints.get(cid) != fp:
                    self._stop_one(cid, reason="参数变更")
                    self._start_one(desired_map[cid], defaults, ai_config, fp)
                    restarted += 1
                else:
                    kept += 1

            if reason:
                self._reload_count += 1
                print(
                    f"[Worker] 热重载 #{self._reload_count} {reason}: "
                    f"保留={kept} 重启={restarted} 新建={started} "
                    f"当前={[m for m in self.monitors]}"
                )

        try:
            self._config_mtime = self.config_path.stat().st_mtime
        except OSError:
            pass

    def _start_one(
        self, channel: Dict, defaults: Dict, ai_config: Dict, fp: str
    ) -> None:
        cid = channel["id"]
        m = StreamMonitor(channel, defaults, str(self.work_dir), ai_config)
        t = m.start_thread()
        self.monitors[cid] = m
        self.threads[cid] = t
        self.fingerprints[cid] = fp
        print(f"[Worker] 启动监测线程: {cid} ({channel.get('url')}) fp={fp}")

    def _stop_one(self, cid: str, reason: str = "") -> None:
        m = self.monitors.pop(cid, None)
        t = self.threads.pop(cid, None)
        self.fingerprints.pop(cid, None)
        if m:
            print(
                f"[Worker] 停止监测线程: {cid}"
                + (f" ({reason})" if reason else "")
            )
            m.stop()
        if t and t.is_alive():
            t.join(timeout=8)

    def stop_all(self) -> None:
        self.running = False
        with self._lock:
            for cid in list(self.monitors.keys()):
                self._stop_one(cid, reason="Worker 退出")

    def run(self) -> None:
        self.reconcile(reason="初始启动")
        if not self.monitors and self.id_filter is None:
            print("[Worker] 当前无启用频道，等待配置…")

        last_check = 0.0
        while self.running:
            time.sleep(0.5)
            # 线程意外退出时，在重载周期内拉起
            now = time.time()
            if not self.enable_reload:
                with self._lock:
                    if self.monitors and not any(
                        t.is_alive() for t in self.threads.values()
                    ):
                        break
                continue

            if now - last_check < self.reload_interval:
                continue
            last_check = now

            try:
                mtime = self.config_path.stat().st_mtime
            except OSError:
                continue

            need = mtime != self._config_mtime
            if not need:
                # 仍检查死亡线程
                with self._lock:
                    dead = [
                        cid
                        for cid, t in self.threads.items()
                        if t is not None and not t.is_alive()
                    ]
                if dead:
                    need = True
            if not need:
                continue

            # 防抖
            time.sleep(0.3)
            try:
                mtime2 = self.config_path.stat().st_mtime
            except OSError:
                continue
            if mtime2 != mtime and mtime != self._config_mtime:
                # 仍在写入
                continue
            self.reconcile(reason="配置变更" if mtime != self._config_mtime else "线程恢复")


def main():
    parser = argparse.ArgumentParser(description="AI 节目监测 Worker")
    parser.add_argument(
        "-c", "--config", default="../config/channels.yaml", help="配置文件路径"
    )
    parser.add_argument("-w", "--workdir", default="..", help="工作目录")
    parser.add_argument("--ids", nargs="+", help="只监测指定的频道 ID")
    parser.add_argument(
        "--reload-interval",
        type=float,
        default=3.0,
        help="配置热重载检查间隔（秒）",
    )
    parser.add_argument(
        "--no-reload",
        action="store_true",
        help="禁用配置热重载",
    )
    args = parser.parse_args()

    runtime = WorkerRuntime(
        config_path=args.config,
        work_dir=args.workdir,
        id_filter=args.ids,
        reload_interval=args.reload_interval,
        enable_reload=not args.no_reload,
    )

    def signal_handler(sig, frame):
        print("\n收到退出信号，正在停止所有监测...")
        runtime.stop_all()

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    mode = "开" if not args.no_reload else "关"
    print(
        f"[Worker] 热重载={mode} 间隔={args.reload_interval}s "
        f"ids={args.ids or '全部启用'}"
    )
    try:
        runtime.run()
    except KeyboardInterrupt:
        signal_handler(None, None)

    print("Worker 已退出")


if __name__ == "__main__":
    main()
