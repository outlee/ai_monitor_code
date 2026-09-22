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
import queue
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections import deque
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

# SQLite 双写（失败不影响主流程）
try:
    _ROOT = Path(__file__).resolve().parent.parent
    if str(_ROOT) not in sys.path:
        sys.path.insert(0, str(_ROOT))
    import event_db as _event_db  # type: ignore
except Exception:
    _event_db = None  # type: ignore


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
        # 业务网卡名（可选）。FFmpeg localaddr 加组失败时，由 Worker 自动按网卡抓包再喂给 FFmpeg
        # 例：iface: enp1s0f1 ；同一组播多 program 共用一个抓包进程
        self.iface = (
            channel.get("iface")
            or defaults.get("iface")
            or ""
        )
        self.iface = str(self.iface).strip() or None
        self._ingest_url = self.url  # 监测 FFmpeg 读取地址
        self._thumb_url = self.url  # 截图专用地址（与监测分端口，避免抢包）
        self._capture_key = None  # (iface, group, port, consumer_id) for release

        self.black_duration = float(
            channel.get("black_duration", defaults.get("black_duration", 3.0))
        )
        self.freeze_duration = float(
            channel.get("freeze_duration", defaults.get("freeze_duration", 12.0))
        )
        # freezedetect 噪声阈值，越大越不敏感（组播环境建议 >= 0.02）
        self.freeze_noise = float(
            channel.get("freeze_noise", defaults.get("freeze_noise", 0.02))
        )
        self.silence_duration = float(
            channel.get("silence_duration", defaults.get("silence_duration", 12.0))
        )
        self.silence_threshold = channel.get(
            "silence_threshold", defaults.get("silence_threshold", -50)
        )
        self.save_snapshot = channel.get(
            "save_snapshot", defaults.get("save_snapshot", True)
        )
        # 分项开关：组播+网卡抓包时默认先关掉无伴音（丢包极易误报）
        self.detect_black = bool(
            channel.get("detect_black", defaults.get("detect_black", True))
        )
        self.detect_freeze = bool(
            channel.get("detect_freeze", defaults.get("detect_freeze", True))
        )
        _def_sil = False if self.iface else True
        self.detect_silence = bool(
            channel.get("detect_silence", defaults.get("detect_silence", _def_sil))
        )
        # 告警确认：ffmpeg 报 start 后还要再持续 confirm 秒且未 end 才正式告警
        self.alarm_confirm_sec = float(
            defaults.get("alarm_confirm_sec", 3.0)
        )
        # 同类告警冷却，避免静帧/恢复来回刷
        self.alarm_cooldown_sec = float(
            defaults.get("alarm_cooldown_sec", 90.0)
        )
        self._pending_alarms = {}  # key -> {event, since}
        self._cooldown_until = {}  # key -> ts

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
        self._last_media_ts = 0.0
        self._run_started_ts = 0.0

        self._state = "init"
        self._active_alarms: Dict[str, float] = {}  # type -> start_ts
        self._status_lock = threading.Lock()
        self._snapshot_inflight = False
        self._snapshot_lock = threading.Lock()
        self._thumb_thread: Optional[threading.Thread] = None
        self._thumb_proc: Optional[subprocess.Popen] = None
        self._thumb_feeder = None
        # 部分旧 ffmpeg 不支持 -max_error_rate；失败一次后关掉
        self._thumb_use_max_error_rate = True
        self._last_status_db_ts = 0.0
        self._status_db_interval = float(
            defaults.get("status_db_interval_sec", 30.0)
        )

        self.logger = logging.getLogger(f"monitor.{self.id}")
        self._setup_logger()

        # SQLite 初始化（每进程一次即可）
        if _event_db is not None:
            try:
                _event_db.configure(self.work_dir)
            except Exception as e:
                self.logger.debug(f"event_db 初始化跳过: {e}")

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

    def _ensure_ingest_url(self) -> str:
        """
        若配置了 iface 且 url 为组播，则自动启动（或复用）网卡抓包进程，
        返回供 FFmpeg 使用的本机 UDP 地址。对使用者而言无需手工开中继。
        """
        if self._capture_key and self._ingest_url:
            return self._ingest_url
        if not self.iface:
            self._ingest_url = self.url
            return self._ingest_url
        try:
            from iface_mcast import resolve_ffmpeg_url
        except ImportError:
            try:
                from workers.iface_mcast import resolve_ffmpeg_url
            except ImportError as e:
                self.logger.error("无法加载 iface_mcast: %s" % e)
                self._ingest_url = self.url
                return self._ingest_url
        try:
            mon_url, thumb_url, key = resolve_ffmpeg_url(
                str(self.work_dir),
                self.url,
                self.iface,
                consumer_id=self.id,
                logger=self.logger,
            )
            self._ingest_url = mon_url
            self._thumb_url = thumb_url or mon_url
            self._capture_key = key
            if key:
                self.logger.info(
                    "已启用网卡收流 iface=%s 原始=%s -> 监测%s 截图%s"
                    % (self.iface, self.url, mon_url, self._thumb_url)
                )
        except Exception as e:
            self.logger.error("网卡收流启动失败: %s" % e)
            self._ingest_url = self.url
            self._thumb_url = self.url
            self._capture_key = None
        return self._ingest_url

    def _release_capture(self):
        if not self._capture_key:
            return
        try:
            from iface_mcast import release
        except ImportError:
            try:
                from workers.iface_mcast import release
            except ImportError:
                release = None
        if release:
            try:
                iface, group, port, consumer_id = self._capture_key
                release(iface, group, port, consumer_id, logger=self.logger)
            except Exception as e:
                self.logger.debug("release capture: %s" % e)
        self._capture_key = None

    def _input_url_with_timeout(self) -> str:
        """
        为 UDP/RTP 注入收包超时，避免无流时 FFmpeg 永久阻塞、无法进入重连。
        超时单位：微秒（FFmpeg udp 协议 timeout 选项）。
        """
        url = self._ensure_ingest_url()
        lower = url.lower()
        if not (lower.startswith("udp:") or lower.startswith("rtp:")):
            return url
        timeout_sec = float(self.defaults.get("input_timeout_sec", 15.0))
        # 本机 fan-out 探测需要更长时间
        if "127.0.0.1" in url or "@:" in url:
            timeout_sec = max(timeout_sec, 45.0)
        timeout_us = int(timeout_sec * 1_000_000)
        extras = []
        if "timeout=" not in url:
            extras.append("timeout=%d" % timeout_us)
        # 不要附加 fifo_size/buffer_size：部分 FFmpeg 会因此一直
        # “Could not detect TS packet size”，解不出流、界面一直无信号。
        if not extras:
            return url
        sep = "&" if "?" in url else "?"
        return url + sep + "&".join(extras)

    def _build_filter_complex(self) -> str:
        """
        视频：降采样 → 规则检测；可选按帧序号旁路 latest.jpg
        音频：可选 silencedetect；关闭时直通

        旁路用 select=mod(n)（按帧号，不看 PTS），避免 fps 滤镜在组播花 PTS 下不出图。
        """
        vin = self._v_label()
        ain = self._a_label()
        vparts = []
        if self.detect_black:
            vparts.append(f"blackdetect=d={self.black_duration}:pix_th=0.10")
        if self.detect_freeze:
            vparts.append(
                f"freezedetect=n={self.freeze_noise}:d={self.freeze_duration}"
            )
        detect = ",".join(vparts) if vparts else "null"
        if self.detect_silence:
            audio = (
                f"[{ain}]silencedetect=noise={self.silence_threshold}dB:"
                f"d={self.silence_duration}[aout]"
            )
        else:
            audio = f"[{ain}]volume=1[aout]"

        dw = self.detect_width
        if dw and dw > 0:
            v = (
                f"[{vin}]scale=w='min(iw\\,{dw})':h=-2:flags=fast_bilinear,"
                f"{detect}[vout]"
            )
        else:
            v = f"[{vin}]{detect}[vout]"
        return f"{v};{audio}"

    def _build_ffmpeg_cmd(self) -> List[str]:
        """规则检测；frame_interval>0 时同一解码器旁路写 latest.jpg。"""
        fc = self._build_filter_complex()
        ingest = self._input_url_with_timeout()
        is_udp = ingest.lower().startswith("udp:")

        cmd: List[str] = [
            "ffmpeg",
            "-hide_banner",
            "-nostats",
            "-loglevel",
            "info",
            "-fflags",
            "+genpts+discardcorrupt+igndts",
            "-err_detect",
            "ignore_err",
            "-max_error_rate",
            "1.0",
        ]
        if is_udp or self.program is not None:
            cmd.extend(
                [
                    "-probesize",
                    "2M",
                    "-analyzeduration",
                    "2M",
                ]
            )
        if is_udp:
            cmd.extend(["-f", "mpegts"])
        cmd.extend(
            [
                "-i",
                ingest,
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
            "ingest_url": self._ingest_url,
            "thumb_url": getattr(self, "_thumb_url", None),
            "iface": self.iface,
            "latest_jpg": str(self.latest_frame_path),
            "latest_exists": bool(
                self.latest_frame_path.is_file()
                and self.latest_frame_path.stat().st_size > 0
            )
            if self.latest_frame_path
            else False,
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
            "last_media_ts": self._last_media_ts or None,
            "media_ok": self._media_ok(),
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
            # 低频写入状态采样，供历史/大屏（默认 30s）
            if (
                _event_db is not None
                and _now_ts() - self._last_status_db_ts >= self._status_db_interval
            ):
                try:
                    _event_db.insert_status_sample(self.id, payload)
                    self._last_status_db_ts = _now_ts()
                except Exception:
                    pass
        except OSError as e:
            self.logger.debug(f"写心跳失败: {e}")

    def _media_timeout_sec(self) -> float:
        return max(float(self.defaults.get("input_timeout_sec", 15.0)), 20.0)

    def _media_ok(self) -> bool:
        # -nostats 时解到流后几乎不再打 Video: 行；只要进程还在且曾经解到过，就算有信号
        if (
            self._last_media_ts
            and self.process is not None
            and self.process.poll() is None
        ):
            return True
        age = self._latest_frame_age()
        if age is not None and age <= self._media_timeout_sec():
            return True
        return False

    def _note_media(self):
        first = not self._last_media_ts
        self._last_media_ts = _now_ts()
        if "no_signal" in self._active_alarms:
            start_ts = self._active_alarms.pop("no_signal", None)
            ev = {
                "type": "no_signal_end",
                "phase": "end",
                "channel_id": self.id,
                "channel_name": self.name,
                "message": "节目信号恢复",
                "time": _now_str(),
            }
            if start_ts:
                ev["duration"] = round(_now_ts() - start_ts, 3)
            self.logger.info(json.dumps(ev, ensure_ascii=False))
            self._save_event(ev)
        if first or self._state == "starting":
            self.logger.info("已解到音视频")
            self._write_status("running")

    def _check_no_signal(self):
        if self._state not in ("running", "starting"):
            return
        if self._media_ok():
            if "no_signal" in self._active_alarms:
                self._note_media()
            return
        age = self._latest_frame_age()
        if age is not None and age <= self._media_timeout_sec():
            self._note_media()
            return
        # 进程还在且持续有 demux 日志（Packet corrupt 也算在收 TS）
        if (
            self.process is not None
            and self.process.poll() is None
            and self._last_ffmpeg_activity_ts
            and (_now_ts() - self._last_ffmpeg_activity_ts) < 15
            and (_now_ts() - (self._run_started_ts or 0)) > 20
        ):
            self._note_media()
            return
        started = getattr(self, "_run_started_ts", 0.0) or _now_ts()
        wait = _now_ts() - (self._last_media_ts or started)
        # 启动探测 MPTS 可能需要较长时间，不要 20s 就标无信号
        grace = 90.0 if self._state == "starting" else self._media_timeout_sec()
        if wait <= grace:
            return
        if "no_signal" in self._active_alarms:
            return
        self._active_alarms["no_signal"] = _now_ts()
        ev = {
            "type": "no_signal",
            "phase": "start",
            "channel_id": self.id,
            "channel_name": self.name,
            "message": "未解到节目流（网卡无载波或无组播数据）",
            "time": _now_str(),
        }
        self.logger.warning(json.dumps(ev, ensure_ascii=False))
        self._save_event(ev)
        self._write_status()

    def _maybe_heartbeat(self):
        self._check_no_signal()
        if _now_ts() - self._last_heartbeat_ts >= self.heartbeat_interval:
            self._write_status()

    def _stop_thumb_proc(self):
        feeder = getattr(self, "_thumb_feeder", None)
        if feeder is not None:
            try:
                feeder.close()
            except Exception:
                pass
            self._thumb_feeder = None
        if self._capture_key:
            try:
                from iface_mcast import unregister_feeder
            except ImportError:
                try:
                    from workers.iface_mcast import unregister_feeder
                except ImportError:
                    unregister_feeder = None
            if unregister_feeder:
                try:
                    iface, group, port, cid = self._capture_key
                    unregister_feeder(iface, group, port, cid)
                except Exception:
                    pass
        if self._thumb_proc is not None and self._thumb_proc.poll() is None:
            try:
                if self._thumb_proc.stdin:
                    try:
                        self._thumb_proc.stdin.close()
                    except Exception:
                        pass
                self._thumb_proc.terminate()
                self._thumb_proc.wait(timeout=3)
            except Exception:
                try:
                    self._thumb_proc.kill()
                except Exception:
                    pass
        self._thumb_proc = None

    def _thumb_udp_url(self) -> str:
        """截图专用本机 UDP（与监测分端口，保留数据报边界）。"""
        self._ensure_ingest_url()
        src = self._thumb_url or self._ingest_url or self.url
        # 不要加 fifo_size（会把 TS 探测搞死）；也不要短 timeout
        return src

    def _start_live_thumb_ffmpeg(self):
        """
        常驻 FFmpeg 读截图专用 UDP 口，覆盖写 latest.jpg。

        注意：不能把多包 TS 拼进 stdin——UDP 每包自带 188 对齐，拼流后一旦
        错位会整路 PES mismatch，表现为喂了上百 MB 仍无图最后 ffmpeg_exit。
        """
        self.snapshot_dir.mkdir(parents=True, exist_ok=True)
        out = str(self.latest_frame_path)
        err_path = self.snapshot_dir / "thumb_ffmpeg.err"
        src = self._thumb_udp_url()

        cmd = [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-nostats",
            "-loglevel",
            "warning",
            "-fflags",
            "+genpts+discardcorrupt+igndts",
            "-err_detect",
            "ignore_err",
            "-skip_frame",
            "nokey",
            "-probesize",
            "2M",
            "-analyzeduration",
            "2M",
        ]
        if src.lower().startswith("udp:"):
            cmd.extend(["-f", "mpegts"])
        cmd.extend(["-i", src])
        if self._thumb_use_max_error_rate:
            cmd.extend(["-max_error_rate", "1.0"])
        if self.program is not None:
            cmd.extend(["-map", "0:p:%d:v:0" % int(self.program)])
        else:
            cmd.extend(["-map", "0:v:0"])
        cmd.extend(
            [
                "-an",
                "-vf",
                "scale=640:-2",
                "-vsync",
                "0",
                "-f",
                "image2",
                "-update",
                "1",
                "-q:v",
                "5",
                out,
            ]
        )

        err_f = open(str(err_path), "w")
        wrapped = list(cmd)
        if shutil.which("stdbuf"):
            wrapped = ["stdbuf", "-oL", "-eL"] + cmd
        self.logger.info(
            "[thumb] start udp_live -> %s src=%s program=%s"
            % (out, src, self.program)
        )
        proc = subprocess.Popen(
            wrapped,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=err_f,
        )
        self._thumb_proc = proc

        logged_ok = False
        last_progress = time.time()
        while (
            self.running
            and self._state in ("running", "starting")
            and proc.poll() is None
        ):
            time.sleep(1.0)
            if (
                not logged_ok
                and self.latest_frame_path.is_file()
                and self.latest_frame_path.stat().st_size > 1024
            ):
                self.logger.info(
                    "[thumb] latest_ok size=%d"
                    % self.latest_frame_path.stat().st_size
                )
                logged_ok = True
            now = time.time()
            if not logged_ok and now - last_progress >= 15:
                try:
                    err_f.flush()
                except Exception:
                    pass
                self.logger.info("[thumb] waiting_frame src=%s" % src)
                last_progress = now

        rc = proc.poll()
        err_tail = ""
        try:
            err_f.flush()
            with open(str(err_path), "r") as rf:
                err_tail = (rf.read() or "")[-500:]
        except Exception:
            pass
        try:
            err_f.close()
        except Exception:
            pass
        if rc is not None:
            self.logger.warning(
                "[thumb] ffmpeg_exit code=%s %s"
                % (rc, err_tail.replace("\n", " ")[:240])
            )
            if self._thumb_use_max_error_rate and "max_error_rate" in err_tail:
                self._thumb_use_max_error_rate = False
                self.logger.warning("[thumb] disable max_error_rate for retry")
        self._stop_thumb_proc()

    def _start_thumb_thread(self):
        """
        刷新 latest.jpg：
        - 有 iface 抓包：常驻 FFmpeg 读 TsFeeder（推荐，解码器保持状态）
        - 否则：周期性一次性 ffmpeg 抽帧
        """
        if self.frame_interval_sec <= 0:
            return
        if self._thumb_thread and self._thumb_thread.is_alive():
            return

        def _loop():
            self.snapshot_dir.mkdir(parents=True, exist_ok=True)
            interval = max(float(self.frame_interval_sec), 2.0)
            fail_streak = 0
            logged_ok = False
            # 优先等主监测 FFmpeg 旁路出图（同一解码器，不另起一路）
            mode = "watch_main" if self._capture_key else "ffmpeg_grab"
            self.logger.info(
                "[thumb] thread_run mode=%s interval=%.1fs -> %s"
                % (mode, interval, self.latest_frame_path)
            )
            wait_main_s = 45.0
            waited = 0.0
            while self.running:
                if self._state not in ("running", "starting"):
                    self._stop_thumb_proc()
                    time.sleep(1.0)
                    waited = 0.0
                    continue

                if (
                    self.latest_frame_path.is_file()
                    and self.latest_frame_path.stat().st_size > 1024
                ):
                    if not logged_ok:
                        self.logger.info(
                            "[thumb] latest_ok size=%d via=main"
                            % self.latest_frame_path.stat().st_size
                        )
                        logged_ok = True
                    fail_streak = 0
                    waited = 0.0
                    time.sleep(interval)
                    continue

                if self._capture_key:
                    try:
                        self.logger.info("[thumb] start udp_live decoder")
                        self._start_live_thumb_ffmpeg()
                    except Exception as e:
                        self.logger.warning("[thumb] udp_live_fail: %s" % e)
                        self._stop_thumb_proc()
                        time.sleep(3.0)
                    waited = 0.0
                    time.sleep(2.0)
                    continue

                # 无 iface：退回周期性抽帧
                ok = False
                try:
                    ok = self._grab_frame_ffmpeg(self.latest_frame_path)
                except Exception as e:
                    self.logger.warning("实时截图刷新异常: %s" % e)
                if ok:
                    fail_streak = 0
                    if not logged_ok:
                        try:
                            sz = self.latest_frame_path.stat().st_size
                        except OSError:
                            sz = 0
                        self.logger.info("latest.jpg 已生成 size=%d" % sz)
                        logged_ok = True
                else:
                    fail_streak += 1
                    if fail_streak <= 3 or fail_streak % 15 == 0:
                        self.logger.warning(
                            "实时截图刷新失败 streak=%d" % fail_streak
                        )
                end = time.time() + interval
                while self.running and time.time() < end:
                    time.sleep(min(0.5, max(0.05, end - time.time())))

        self._thumb_thread = threading.Thread(
            target=_loop, name="thumb-%s" % self.id, daemon=True
        )
        self._thumb_thread.start()
        self.logger.info("[thumb] thread_started -> %s" % self.latest_frame_path)

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
            # 双写 SQLite（历史查询 / 大屏统计）
            if _event_db is not None:
                try:
                    _event_db.insert_alert(event)
                except Exception as e:
                    self.logger.debug(f"SQLite 写事件跳过: {e}")
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

    def _thumb_input_url(self) -> str:
        """截图专用输入（与监测 FFmpeg 分端口）。"""
        self._ensure_ingest_url()
        src = self._thumb_url or self._ingest_url or self.url
        if not src.lower().startswith(("udp:", "rtp:")):
            return src
        if "timeout=" in src:
            return src
        timeout_us = int(max(float(self.defaults.get("input_timeout_sec", 15.0)), 15.0) * 1_000_000)
        sep = "&" if "?" in src else "?"
        return "%s%stimeout=%d" % (src, sep, timeout_us)

    def _grab_frame_ffmpeg(
        self, out_path: Path, quality: int = 3, *, keyframe_only: bool = False
    ) -> bool:
        """独立 FFmpeg 抽 1 帧。使用截图专用端口，不与主监测抢包。"""
        try:
            src = self._thumb_input_url()
            self.snapshot_dir.mkdir(parents=True, exist_ok=True)
            cmd = [
                "ffmpeg",
                "-y",
                "-hide_banner",
                "-loglevel",
                "error",
                "-fflags",
                "+genpts+discardcorrupt",
                "-rw_timeout",
                "10000000",
                "-probesize",
                "8M",
                "-analyzeduration",
                "3M",
            ]
            if src.lower().startswith("udp:"):
                cmd.extend(["-f", "mpegts"])
            cmd.extend(["-i", src])
            if self.program is not None:
                cmd.extend(["-map", "0:p:%d:v" % int(self.program)])
            else:
                cmd.extend(["-map", "0:v:0"])
            if keyframe_only:
                cmd.extend(["-skip_frame", "nokey"])
            tmp = out_path.parent / (".grab_%s_%s.tmp.jpg" % (self.id, out_path.stem))
            cmd.extend(
                [
                    "-frames:v",
                    "1",
                    "-vf",
                    "scale=640:-2",
                    "-q:v",
                    str(quality),
                    str(tmp),
                ]
            )
            r = subprocess.run(
                cmd,
                timeout=15,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                universal_newlines=True,
            )
            ok = tmp.is_file() and tmp.stat().st_size > 0
            if not ok:
                if r.stderr:
                    err = (r.stderr or "").strip().splitlines()
                    if err:
                        self.logger.warning(
                            "抽帧失败 src=%s: %s" % (src, err[-1][:200])
                        )
                try:
                    if tmp.is_file():
                        tmp.unlink()
                except OSError:
                    pass
                return False
            os.replace(str(tmp), str(out_path))
            return True
        except Exception as e:
            self.logger.warning("独立抽帧异常: %s" % e)
            return False

    def _take_snapshot(self, event_type: str) -> Optional[Path]:
        """
        告警截图策略（降低花屏误截）：
        - 无伴音/断流：默认不截（画面参考价值低，且易截到损坏帧）
        - 黑场/静帧：优先用很新的旁路 latest；否则后台抽关键帧
        """
        # 断流/结束事件不截；黑场/静帧/无伴音等异常仍截（保留异常截图）
        if event_type in ("stream_down",) or str(event_type).endswith("_end"):
            return None

        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        out_path = self.snapshot_dir / f"{event_type}_{ts}.jpg"

        def _unlink_quiet(p):
            try:
                if p.is_file():
                    os.unlink(str(p))
            except OSError:
                pass

        # 实时 latest 够新则直接复制（黑场/静帧/AI）；无伴音也可留一张当时画面
        age = self._latest_frame_age()
        max_age = max(float(self.frame_interval_sec) * 2.5, 3.0)
        visual = event_type in ("black", "freeze") or str(event_type).startswith(
            "ai_"
        )
        prefer = (
            self.snapshot_prefer_latest
            and age is not None
            and age <= max_age
            and (visual or event_type == "silence")
        )
        if prefer and self._copy_latest_frame(out_path):
            try:
                if out_path.stat().st_size >= 8 * 1024:
                    self.logger.info(f"截图已保存(旁路): {out_path}")
                    self._prune_snapshots()
                    return out_path
            except OSError:
                pass
            _unlink_quiet(out_path)

        if not visual:
            return None

        def _bg():
            with self._snapshot_lock:
                if self._snapshot_inflight:
                    return
                self._snapshot_inflight = True
            try:
                time.sleep(0.5)
                if self._copy_latest_frame(out_path):
                    try:
                        if out_path.stat().st_size >= 8 * 1024:
                            self.logger.info(f"截图已保存(旁路延迟): {out_path}")
                            self._prune_snapshots()
                            return
                    except OSError:
                        pass
                    _unlink_quiet(out_path)
                if self._grab_frame_ffmpeg(out_path, quality=3, keyframe_only=True):
                    try:
                        if out_path.stat().st_size < 8 * 1024:
                            _unlink_quiet(out_path)
                            self.logger.warning("截图过小已丢弃（可能花屏）")
                            return
                    except OSError:
                        pass
                    self.logger.info(f"截图已保存(关键帧): {out_path}")
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

    def _commit_alarm_start(self, alarm_key: str, event: Dict):
        """真正落库的开始告警（已过确认期）。"""
        now = _now_ts()
        cool = self._cooldown_until.get(alarm_key, 0)
        if now < cool:
            self.logger.info(
                "告警冷却中，忽略 %s（还需 %.0fs）"
                % (alarm_key, cool - now)
            )
            return
        self._active_alarms[alarm_key] = now
        self._cooldown_until[alarm_key] = now + self.alarm_cooldown_sec
        if self.save_snapshot:
            snap = self._take_snapshot(event["type"])
            if snap:
                event["snapshot"] = str(snap)
        self.logger.warning(json.dumps(event, ensure_ascii=False))
        self._save_event(event)
        self._write_status()

    def _flush_pending_alarms(self):
        """确认期内仍未收到 end 的 pending → 正式告警。"""
        if not self._pending_alarms:
            return
        now = _now_ts()
        done = []
        for key, item in list(self._pending_alarms.items()):
            if now - item["since"] >= self.alarm_confirm_sec:
                self._commit_alarm_start(key, item["event"])
                done.append(key)
        for key in done:
            self._pending_alarms.pop(key, None)

    def _emit_alarm_event(
        self,
        *,
        alarm_key: str,
        is_start: bool,
        is_end: bool,
        event: Dict,
    ):
        # 开始：先进入确认队列，避免组播抖动/瞬间误报
        if is_start and alarm_key:
            if alarm_key in self._active_alarms:
                return
            if alarm_key not in self._pending_alarms:
                self._pending_alarms[alarm_key] = {
                    "event": event,
                    "since": _now_ts(),
                }
                self.logger.info(
                    "待确认告警 %s（%.1fs 内若恢复则不计）"
                    % (alarm_key, self.alarm_confirm_sec)
                )
            return

        # 结束
        if is_end and alarm_key:
            # 还在确认期：直接撤销，不记恢复事件
            if alarm_key in self._pending_alarms:
                self._pending_alarms.pop(alarm_key, None)
                self.logger.info("告警未确认已撤销: %s" % alarm_key)
                return
            if alarm_key not in self._active_alarms:
                return
            start_ts = self._active_alarms.pop(alarm_key, None)
            if start_ts and "duration" not in event:
                event["duration"] = round(_now_ts() - start_ts, 3)
            self.logger.info(json.dumps(event, ensure_ascii=False))
            self._save_event(event)
            self._write_status()
            return

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
        # 真正解到流的标志，不含 Packet corrupt / 打开失败
        if (
            "stream mapping" in lower
            or "stream map" in lower
            or "stream #" in lower
            or "input #0" in lower
            or "video:" in lower
            or "audio:" in lower
            or "h264" in lower
            or "mpeg2video" in lower
            or "hevc" in lower
            or "black_start" in lower
            or "freeze_start" in lower
            or "silence_start" in lower
            or "packet corrupt" in lower
            or "mpegts" in lower
            or lower.startswith("frame=")
        ):
            self._note_media()

        # 按类型检查；同一行可先后发出 start 与 end
        pairs = []
        if self.detect_black:
            pairs.append(("black", "black_start", "black_end", "黑场"))
        if self.detect_freeze:
            pairs.append(("freeze", "freeze_start", "freeze_end", "静帧"))
        if self.detect_silence:
            pairs.append(("silence", "silence_start", "silence_end", "无伴音"))
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
                        "message": f"检测到{label}",
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
                    "message": f"{label}已恢复"
                    + (f"（持续 {dur:.1f} 秒）" if dur is not None else ""),
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
        self._flush_pending_alarms()
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
        ingest = self._ensure_ingest_url()
        cmd = self._build_ffmpeg_cmd()
        prog = f" program={self.program}" if self.program is not None else ""
        iface_s = f" iface={self.iface}" if self.iface else ""
        self.logger.info(
            f"启动 FFmpeg 监测: {self.name} ({self.url} -> {ingest}){prog}{iface_s} "
            f"detect_width={self.detect_width} "
            f"frame_interval={self.frame_interval_sec}s"
        )
        # 管道不是 TTY 时 glibc 会块缓冲 stderr，Stream 信息要等退出才刷出，
        # 界面就会一直「无信号」。用 stdbuf 强制行缓冲。
        wrapped = list(cmd)
        if shutil.which("stdbuf"):
            wrapped = ["stdbuf", "-oL", "-eL"] + cmd
        self.logger.info("[ffmpeg] %s", " ".join(cmd))
        self.process = subprocess.Popen(
            wrapped,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            bufsize=0,
        )
        self._last_media_ts = 0.0
        self._run_started_ts = _now_ts()
        self._write_status("starting")
        self._start_ai_thread()
        self._start_thumb_thread()
        run_started = self._run_started_ts
        last_lines = deque(maxlen=12)

        assert self.process.stderr is not None
        err_q: "queue.Queue[Optional[str]]" = queue.Queue()

        def _drain_stderr():
            buf = b""
            try:
                while True:
                    chunk = self.process.stderr.read(4096)
                    if not chunk:
                        break
                    buf += chunk
                    while b"\n" in buf:
                        raw, buf = buf.split(b"\n", 1)
                        err_q.put(raw.decode("utf-8", "replace"))
            except Exception:
                pass
            if buf:
                err_q.put(buf.decode("utf-8", "replace"))
            err_q.put(None)

        drainer = threading.Thread(
            target=_drain_stderr, name="fferr-%s" % self.id, daemon=True
        )
        drainer.start()

        while self.running:
            if self.reconnect_count and _now_ts() - run_started >= 60:
                self.reconnect_count = 0
            try:
                line = err_q.get(timeout=0.5)
            except queue.Empty:
                if self.process.poll() is not None and err_q.empty():
                    break
                self._flush_pending_alarms()
                self._maybe_run_ai_inline()
                self._maybe_heartbeat()
                continue
            if line is None:
                break
            last_lines.append(line.rstrip())
            self._parse_ffmpeg_line(line)
            self._maybe_run_ai_inline()
            self._maybe_heartbeat()

        rc = self.process.returncode if self.process else -1
        self.process = None
        self._last_run_sec = _now_ts() - run_started
        tail = " | ".join([x for x in last_lines if x])[-400:]
        self.logger.info(
            "FFmpeg 退出 code=%s lived=%.1fs state=%s %s"
            % (rc, self._last_run_sec, self._state, tail)
        )
        return rc if rc is not None else -1

    def run(self):
        """主循环：监测 + 断流重连（在独立线程中调用）。"""
        if not self.enabled:
            self.logger.info(f"频道 {self.name} 已禁用，跳过")
            self._write_status("disabled")
            return

        self.running = True
        delay = self.reconnect_delay
        self._last_run_sec = 0.0
        self.logger.info(f"监测线程启动: {self.name} ({self.url})")
        self._start_ai_thread()

        while self.running:
            try:
                rc = self._run_once()
            except Exception as e:
                self.logger.error(f"监测异常: {e}")
                rc = -1
                self._last_run_sec = 0.0

            if not self.running:
                break

            if rc in (-15, -9):
                break

            # 曾稳定跑过则把退避清零，避免「每 2 分钟闪断却要等 60 秒才重连」
            if self._last_run_sec >= 45:
                delay = self.reconnect_delay
                self.reconnect_count = 0

            self.reconnect_count += 1
            self._active_alarms.clear()
            self._write_status("reconnecting")

            # 断流也要确认：连续失败达到阈值才记告警，避免偶发抖动误报
            down_confirm = int(self.defaults.get("stream_down_confirm", 2))
            if self.reconnect_count >= down_confirm:
                cool_key = "stream_down"
                now = _now_ts()
                if now >= self._cooldown_until.get(cool_key, 0):
                    event = {
                        "type": "stream_down",
                        "phase": "start",
                        "channel_id": self.id,
                        "channel_name": self.name,
                        "message": (
                            f"节目流中断，{delay:.0f} 秒后第 "
                            f"{self.reconnect_count} 次重连"
                        ),
                        "returncode": rc,
                        "reconnect_count": self.reconnect_count,
                        "time": _now_str(),
                    }
                    self.logger.warning(json.dumps(event, ensure_ascii=False))
                    self._save_event(event)
                    self._cooldown_until[cool_key] = now + self.alarm_cooldown_sec
                else:
                    self.logger.info(
                        f"断流抖动忽略（冷却中）第 {self.reconnect_count} 次, code={rc}"
                    )
            else:
                self.logger.info(
                    f"短暂中断，暂不计告警（{self.reconnect_count}/{down_confirm}）code={rc}"
                )

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
        self._stop_thumb_proc()
        self._stop_ffmpeg()
        self._release_capture()
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
