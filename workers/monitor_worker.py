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
import select
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


def _is_alarm_snapshot_name(name: str) -> bool:
    try:
        from web.snapshot_names import is_alarm_snapshot
    except ImportError:
        low = (name or "").strip().lower()
        if not low.endswith(".jpg") or low.startswith("latest") or low.startswith("."):
            return False
        return "/" not in name and "\\" not in name
    return is_alarm_snapshot(name)


def alarm_tail_span_sec(anomaly_age_sec: float) -> float:
    """告警截图只覆盖异常已经持续的后半段，避免解到异常前的正常画面。"""
    try:
        age = float(anomaly_age_sec or 0)
    except (TypeError, ValueError):
        age = 0.0
    if age <= 0:
        return 1.0
    if age < 1.5:
        return max(0.4, age * 0.65)
    return min(2.0, age * 0.45)


def alarm_tail_spans(anomaly_age_sec: float) -> List[float]:
    """先取短尾。短尾没有关键帧时再放宽，第二段仍然落在异常时段里。"""
    primary = alarm_tail_span_sec(anomaly_age_sec)
    spans = [primary]
    try:
        age = float(anomaly_age_sec or 0)
    except (TypeError, ValueError):
        age = 0.0
    if age >= 3.0:
        wider = min(age * 0.75, 3.5)
        if wider >= primary + 0.5:
            spans.append(wider)
    return spans


def alarm_tail_nbytes(bitrate_kbps: float, span_sec: float) -> int:
    """按码率把秒数换成字节。不知道码率时按 2.5Mbps 估，宁短勿把正常画面卷进来。"""
    try:
        kbps = float(bitrate_kbps or 0)
    except (TypeError, ValueError):
        kbps = 0.0
    try:
        span = float(span_sec or 0)
    except (TypeError, ValueError):
        span = 1.0
    if span <= 0:
        span = 1.0
    if kbps < 200:
        kbps = 2500.0
    nbytes = int(kbps * 1000.0 / 8.0 * span)
    return max(188 * 40, min(nbytes, 3 * 1024 * 1024))


_THUMB_TAIL_SEC = 8.0
_THUMB_TAIL_MIN = 12 * 1024 * 1024
_THUMB_TAIL_MAX = 40 * 1024 * 1024


def thumb_tail_nbytes(bitrate_kbps: float) -> int:
    """实时截图用的尾部。按整路码率留约 8 秒，至少 12MB，最多 40MB。

    不知道码率时保持 12MB。慢流本来就盖得住好几秒，不必把几十 MB 送去解。
    """
    try:
        kbps = float(bitrate_kbps or 0)
    except (TypeError, ValueError):
        kbps = 0.0
    if kbps < 200:
        return _THUMB_TAIL_MIN
    nbytes = int(kbps * 1000.0 / 8.0 * _THUMB_TAIL_SEC)
    if nbytes < _THUMB_TAIL_MIN:
        return _THUMB_TAIL_MIN
    if nbytes > _THUMB_TAIL_MAX:
        return _THUMB_TAIL_MAX
    return nbytes


_FRAME_REJECT_REASONS = (
    "smear",
    "conceal",
    "green",
    "flat",
    "gray",
    "too_small",
    "undecodable",
    "not_jpeg",
    "bad_rgb",
)


def thumb_video_maps(program):
    """有节目号时只抽这一套。0:v:0 是整路第一套，会把旁边的节目贴到这张卡片上。"""
    if program is not None:
        p = int(program)
        return [
            ["-map", "0:p:%d:v:0" % p],
            ["-map", "0:p:%d:v" % p],
            ["-map", "0:p:%d:v:1" % p],
        ]
    return [["-map", "0:v:0"], ["-map", "0:v:1"], []]


def map_is_program(maps) -> bool:
    """0:p:N 是这一套节目。0:v:0 是整路里的第一路，会抽到旁边的节目。"""
    for item in maps or []:
        if isinstance(item, str) and item.startswith("0:p:"):
            return True
    return False


def skip_other_program_map(maps, program_frame_seen: bool) -> bool:
    """本节目已经解出过画面时，不再改去抽别的节目。"""
    return bool(program_frame_seen) and not map_is_program(maps)


def alarm_ring_tail(data: bytes, bitrate_kbps: float, span_sec: float) -> bytes:
    """只留缓冲最新的一段。解的是这段里的第一帧，所以段不能伸到异常开始之前。"""
    if not data:
        return b""
    n = alarm_tail_nbytes(bitrate_kbps, span_sec)
    if len(data) <= n:
        return data
    return data[-n:]


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
# 每进程同时只解 1 路过期 TS，6 个 worker 最多 6 路 gst
_gst_thumb_sem = threading.Semaphore(1)


def _now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _now_ts() -> float:
    return time.time()


# 监测网口无载波满这么久，才把该网卡上的节目记为中断。
# 载波刚回来但还没有新的收包窗口时，仍保持中断。
LINK_DOWN_CONFIRM_SEC = 5.0


def capture_link_down(stats, now) -> bool:
    """载波已经落下，并且落下的时间达到确认窗。"""
    if not stats or stats.get("carrier") is not False:
        return False
    since = stats.get("carrier_down_since")
    if since is None:
        return False
    try:
        age = float(now) - float(since)
    except (TypeError, ValueError):
        return False
    return age >= LINK_DOWN_CONFIRM_SEC


def link_blocks_media(stats, now) -> bool:
    """无载波已确认，或载波已回但这一窗口还没有新包。"""
    if capture_link_down(stats, now):
        return True
    if not stats or not stats.get("link_was_down"):
        return False
    if stats.get("carrier") is False:
        return False
    try:
        rate = float(stats.get("pkt_rate") or 0)
    except (TypeError, ValueError):
        rate = 0.0
    return rate <= 0


def capture_status_rates(cap):
    """写心跳用的码率。网口落下或尚未重新收到包时，卡片上的数归零。"""
    if not cap:
        return None
    try:
        rate = float(cap.get("pkt_rate") or 0)
    except (TypeError, ValueError):
        rate = 0.0
    held = cap.get("carrier") is False or (
        bool(cap.get("link_was_down")) and rate <= 0 and cap.get("carrier") is not False
    )
    if held:
        return {
            "pkt_rate": 0,
            "bitrate_kbps": 0,
            "program_bitrate_kbps": 0,
        }
    out = {
        "pkt_rate": cap.get("pkt_rate"),
        "bitrate_kbps": cap.get("bitrate_kbps"),
    }
    if cap.get("program_bitrate_kbps") is not None:
        out["program_bitrate_kbps"] = cap.get("program_bitrate_kbps")
    return out


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
        try:
            from freeze_rules import effective_freeze_seconds, normalize_freeze_mode
        except ImportError:
            from workers.freeze_rules import (  # type: ignore
                effective_freeze_seconds,
                normalize_freeze_mode,
            )
        self.freeze_mode = normalize_freeze_mode(
            channel.get("freeze_mode", defaults.get("freeze_mode", "video"))
        )
        self.freeze_duration = effective_freeze_seconds(
            self.freeze_mode,
            channel.get("freeze_duration", defaults.get("freeze_duration", 12.0)),
        )
        # freezedetect n：平均绝对差 / 256。FFmpeg 默认 0.001。
        # 越大越容易把微动画面判成静帧（0.08 ≈ 20 灰阶，电视剧定镜头必误报）。
        self.freeze_noise = float(
            channel.get("freeze_noise", defaults.get("freeze_noise", 0.003))
        )
        if self.freeze_noise < 0.001:
            self.freeze_noise = 0.001
        if self.freeze_noise > 0.01:
            self.freeze_noise = 0.01
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
        # 没有单独时长的告警才用这个兜底。黑场 / 静帧 / 无伴音各自用配置里的秒数。
        self.alarm_confirm_sec = float(
            defaults.get("alarm_confirm_sec", 5.0)
        )
        if self.alarm_confirm_sec < 0.5:
            self.alarm_confirm_sec = 0.5
        # 静帧确认秒数就是填写的静帧时长。不再被 alarm_confirm_sec 或另一条下限抬高。
        self.freeze_confirm_sec = float(self.freeze_duration)
        try:
            self.freeze_startup_ignore_sec = float(
                channel.get(
                    "freeze_startup_ignore_sec",
                    defaults.get("freeze_startup_ignore_sec", 20),
                )
            )
        except (TypeError, ValueError):
            self.freeze_startup_ignore_sec = 20.0
        if self.freeze_startup_ignore_sec < 0:
            self.freeze_startup_ignore_sec = 0.0
        if self.freeze_startup_ignore_sec > 120:
            self.freeze_startup_ignore_sec = 120.0
        # FFmpeg 滤镜 d= 只做最多 1 秒的短触发，墙钟再补到配置时长，避免两段相加。
        self._black_detect_d = self._probe_seconds(self.black_duration)
        self._freeze_detect_d = self._probe_seconds(self.freeze_duration)
        self._silence_probe_d = self._probe_seconds(self.silence_duration)
        self._audio_unavailable = False
        self._silence_active = False
        self._silence_since = None
        self._audio_db: Optional[float] = None
        self._audio_db_ts = 0.0
        self._audio_status_ts = 0.0
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
        self._latest_valid = False
        self._last_bad_thumb_log = 0.0


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
        self._ring_grab_lock = threading.Lock()
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
        self._ai_bad_since = None
        self._ai_cool_until = 0.0
        self._ai_last_ident = None
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

    @staticmethod
    def _probe_seconds(seconds: float) -> float:
        """滤镜 d= 最长 1 秒。配置时长由墙钟确认，探针秒数从起点里扣回。"""
        try:
            d = float(seconds)
        except (TypeError, ValueError):
            d = 1.0
        if d < 0.1:
            d = 0.1
        if d > 1.0:
            d = 1.0
        return d

    def _alarm_need_sec(self, key: str) -> float:
        """这条告警要持续的墙钟秒数，就是配置里的对应时长。"""
        if key == "freeze":
            return float(self.freeze_duration)
        if key == "black":
            return float(self.black_duration)
        if key == "silence":
            return float(self.silence_duration)
        return float(self.alarm_confirm_sec)

    def _detector_lead_sec(self, key: str) -> float:
        """FFmpeg 报 start 时，异常已经持续了探针这么久。"""
        if key == "freeze":
            return float(self._freeze_detect_d)
        if key == "black":
            return float(self._black_detect_d)
        if key == "silence":
            return float(self._silence_probe_d)
        return 0.0

    def _build_filter_complex(self) -> str:
        """
        视频：降采样 → 规则检测；可选按帧序号旁路 latest.jpg
        音频：可选 silencedetect；关闭时直通

        旁路用 select=mod(n)（按帧号，不看 PTS），避免 fps 滤镜在组播花 PTS 下不出图。
        """
        vin = self._v_label()
        ain = self._a_label()
        vparts = []
        # 按帧号重打单调 PTS（25fps 广播）。不要用流 PTS / demux wallclock：
        # PCR 跳变会让 freezedetect 瞬间 freeze_start，freeze_end 却只有 0.4～3s。
        vparts.append("setpts=N/25/TB")
        if self.detect_black:
            vparts.append(
                "blackdetect=d=%.1f:pix_th=0.10" % self._black_detect_d
            )
        if self.detect_freeze:
            vparts.append(
                "freezedetect=n=%s:d=%.1f"
                % (self.freeze_noise, self._freeze_detect_d)
            )
        detect = ",".join(vparts) if vparts else "null"
        meter = self.detect_silence or (
            self.detect_freeze and self.freeze_mode == "video_silence"
        )
        if meter and self._audio_unavailable:
            # 这一路没有音频 PID。静帧退回只看画面，图仍然要有一路音频输出。
            # 不在空音频上计量，避免音柱一直跳。
            audio = "anullsrc=channel_layout=stereo:sample_rate=8000[aout]"
        elif meter:
            audio = (
                "[%s]silencedetect=noise=%sdB:d=%.1f,%s[aout]"
                % (
                    ain,
                    self.silence_threshold,
                    self._silence_probe_d,
                    self._audio_level_filter(),
                )
            )
        else:
            audio = "[%s]%s[aout]" % (ain, self._audio_level_filter())

        dw = self.detect_width
        # 监测只走 null。同一张图上再挂 FIFO/image2/pipe:1 时，这台
        # FFmpeg 7 对直播 mpegts 的第二路从不写出（fd 都不打开），
        # 还可能把拉帧拖成 frame=0。截图改走 TS ring 慢速兜底。
        if dw and dw > 0:
            v = (
                f"[{vin}]scale=w='min(iw\\,{dw})':h=-2:flags=fast_bilinear,"
                f"{detect}[vout]"
            )
        else:
            v = f"[{vin}]{detect}[vout]"
        return f"{v};{audio}"

    @staticmethod
    def _audio_level_filter() -> str:
        """峰值电平。每 0.5 秒只打一两行，不按音频帧刷日志。"""
        return (
            "astats=metadata=1:reset=0:length=0.3:"
            "measure_perchannel=none:measure_overall=Peak_level,"
            "ametadata=mode=print:key=lavfi.astats.Overall.Peak_level:"
            "enable='lt(mod(t\\,0.5)\\,0.03)'"
        )

    def _audio_level_value(self) -> Optional[float]:
        if self._audio_unavailable or self._audio_db is None:
            return None
        return self._audio_db

    def _audio_state(self) -> str:
        if self._audio_unavailable:
            return "none"
        if not self._audio_db_ts or (_now_ts() - self._audio_db_ts) > 8.0:
            return "idle"
        return "level"

    def _note_peak_line(self, line: str) -> bool:
        """读到峰值就更新音柱。返回 True 表示这行不是告警。"""
        matched = self._RE_PEAK.search(line)
        if not matched:
            return False
        raw = matched.group(1).lower()
        if raw == "nan":
            return True
        if raw.endswith("inf"):
            db = 0.0 if raw.startswith("+") else -120.0
        else:
            try:
                db = float(raw)
            except ValueError:
                return True
        if db > 0:
            db = 0.0
        if db < -120:
            db = -120.0
        self._audio_db = round(db, 1)
        self._audio_db_ts = _now_ts()
        now = self._audio_db_ts
        if now - self._audio_status_ts >= 1.0:
            self._audio_status_ts = now
            self._write_status()
        return True

    def _build_ffmpeg_cmd(self) -> List[str]:
        """规则检测；frame_interval>0 时同一解码器旁路写 latest.jpg。"""
        fc = self._build_filter_complex()
        ingest = self._input_url_with_timeout()
        is_udp = ingest.lower().startswith("udp:")

        cmd: List[str] = [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-nostats",
            "-loglevel",
            "info",
            "-fflags",
            "+genpts+discardcorrupt",
            "-err_detect",
            "ignore_err",
            "-max_error_rate",
            "1.0",
            "-threads",
            "1",
            "-filter_threads",
            "1",
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
                "/dev/null",
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
                and self.latest_frame_path.stat().st_size > 1024
            )
            if self.latest_frame_path
            else False,
            "latest_valid": bool(getattr(self, "_latest_valid", False)),
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
            "audio_db": self._audio_level_value(),
            "audio_state": self._audio_state(),
            "audio_db_ts": self._audio_db_ts or None,
        }
        cap = self._capture_stats()
        if cap:
            rates = capture_status_rates(cap) or {}
            payload["pkt_rate"] = rates.get("pkt_rate")
            payload["bitrate_kbps"] = rates.get("bitrate_kbps")
            if "program_bitrate_kbps" in rates:
                payload["program_bitrate_kbps"] = rates.get("program_bitrate_kbps")
            payload["capture_skip"] = cap.get("skip")
            payload["capture_dests"] = cap.get("dests")
            payload["capture_ring_kb"] = cap.get("ring_kb")
            payload["capture_group"] = cap.get("group")
            payload["capture_port"] = cap.get("mport")
            if "carrier" in cap:
                payload["capture_carrier"] = cap.get("carrier")
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

    def _capture_stats(self):
        if not self._capture_key:
            return {}
        try:
            from iface_mcast import hub_stats, pick_program_bitrate
        except ImportError:
            try:
                from workers.iface_mcast import hub_stats, pick_program_bitrate
            except ImportError:
                return {}
        try:
            iface, group, port, _cid = self._capture_key
        except Exception:
            return {}
        try:
            st = hub_stats(iface, group, port) or {}
        except Exception:
            return {}
        if not st:
            return {}
        chosen = pick_program_bitrate(st.get("programs") or {}, self.program)
        if chosen is not None:
            st = dict(st)
            st["program_bitrate_kbps"] = chosen
        return st

    def _media_timeout_sec(self) -> float:
        return max(float(self.defaults.get("input_timeout_sec", 15.0)), 20.0)

    def _link_blocks(self) -> bool:
        return link_blocks_media(self._capture_stats(), _now_ts())

    def _media_ok(self) -> bool:
        # 监测网口无载波时，进程还活着、缓冲里还有旧画面，也不算有信号
        if self._link_blocks():
            return False
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

    def _emit_link_down(self):
        if "no_signal" in self._active_alarms:
            return
        self._active_alarms["no_signal"] = _now_ts()
        iface = (self.iface or "").strip()
        if iface:
            message = "监测网口 %s 无载波，节目中断" % iface
        else:
            message = "监测网口无载波，节目中断"
        ev = {
            "type": "no_signal",
            "phase": "start",
            "channel_id": self.id,
            "channel_name": self.name,
            "message": message,
            "time": _now_str(),
        }
        self.logger.warning(json.dumps(ev, ensure_ascii=False))
        self._save_event(ev)
        # 启动后网口已经是断的，不能一直停在「探测中」
        self._write_status("running")

    def _check_no_signal(self):
        if self._state not in ("running", "starting"):
            return
        # 确认窗已经在 link_blocks_media 里算过，这里立刻记一条，不再看旧画面
        if self._link_blocks():
            self._emit_link_down()
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
        return src

    def _gst_pipeline_cmd(self, gst, ts_path, jpg_tmp, kind):
        cmd = [
            gst,
            "-q",
            "filesrc",
            "location=%s" % ts_path,
            "!",
            "tsdemux",
        ]
        if self.program is not None:
            cmd.append("program-number=%d" % int(self.program))
        if kind == "h264":
            cmd.extend(["!", "h264parse", "!", "avdec_h264"])
        elif kind == "mpeg2":
            cmd.extend(["!", "mpegvideoparse", "!", "avdec_mpeg2video"])
        else:
            cmd.extend(["!", "decodebin"])
        cmd.extend(
            [
                "!",
                "videoconvert",
                "!",
                "videoscale",
                "!",
                "video/x-raw,width=320,height=180",
                "!",
                "jpegenc",
                "quality=70",
                "!",
                "multifilesink",
                "location=%s" % jpg_tmp,
                "max-files=1",
            ]
        )
        return cmd

    def _gst_run_until_jpeg(self, cmd, jpg_tmp, wait_sec, env):
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            env=env,
        )
        deadline = time.time() + float(wait_sec)
        try:
            while time.time() < deadline:
                if proc.poll() is not None:
                    break
                try:
                    if jpg_tmp.is_file() and jpg_tmp.stat().st_size > 2048:
                        break
                except OSError:
                    pass
                time.sleep(0.05)
        finally:
            if proc.poll() is None:
                try:
                    proc.terminate()
                    proc.wait(timeout=1.2)
                except Exception:
                    try:
                        proc.kill()
                    except Exception:
                        pass
        err = b""
        try:
            if proc.stderr:
                err = proc.stderr.read() or b""
        except Exception:
            pass
        last = err.decode("utf-8", "replace").strip().splitlines()
        last = last[-1][:180] if last else ""
        return last

    def _gst_grab_jpeg(self, ts_path: Path, jpg_tmp: Path):
        """tsdemux 抽第一帧即停。排队拿全机锁，缺图频道也能轮到。"""
        gst = shutil.which("gst-launch-1.0") or "/usr/bin/gst-launch-1.0"
        if not os.path.isfile(gst):
            return False, "no_gst"
        env = os.environ.copy()
        env["GST_DEBUG"] = "0"
        lock_path = self.work_dir / "logs" / ".gst_thumb.lock"
        lf = None
        last = ""
        try:
            self.log_dir.mkdir(parents=True, exist_ok=True)
            lf = open(str(lock_path), "a+")
            lock_deadline = time.time() + 45.0
            while True:
                try:
                    fcntl.flock(lf.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except (IOError, OSError):
                    if time.time() >= lock_deadline:
                        return False, "gst_busy"
                    time.sleep(0.25)
            with _gst_thumb_sem:
                for kind in ("h264", "decode"):
                    try:
                        if jpg_tmp.is_file():
                            jpg_tmp.unlink()
                    except OSError:
                        pass
                    cmd = self._gst_pipeline_cmd(gst, ts_path, jpg_tmp, kind)
                    last = self._gst_run_until_jpeg(cmd, jpg_tmp, 8.0, env)
                    if jpg_tmp.is_file() and jpg_tmp.stat().st_size > 2048:
                        break
        except Exception as e:
            return False, str(e)[:120]
        finally:
            if lf is not None:
                try:
                    fcntl.flock(lf.fileno(), fcntl.LOCK_UN)
                except Exception:
                    pass
                try:
                    lf.close()
                except Exception:
                    pass
        if not (jpg_tmp.is_file() and jpg_tmp.stat().st_size > 2048):
            return False, last or "gst_no_frame"
        try:
            blob = jpg_tmp.read_bytes()
        except OSError:
            return False, last or "gst_read"
        if not self._is_complete_jpeg(blob):
            return False, "gst_not_jpeg"
        try:
            from frame_quality import jpeg_looks_displayable
        except ImportError:
            try:
                from workers.frame_quality import jpeg_looks_displayable
            except ImportError:
                jpeg_looks_displayable = None
        if jpeg_looks_displayable is not None:
            good, reason = jpeg_looks_displayable(blob)
            if not good:
                self._stash_rejected_thumb(blob, reason)
                return False, reason or last
        return True, last

    def _refresh_latest_from_ring(self) -> bool:
        lock = getattr(self, "_ring_grab_lock", None)
        if lock is None:
            lock = threading.Lock()
            self._ring_grab_lock = lock
        if not lock.acquire(timeout=20):
            self.logger.warning("[thumb] 收包缓冲正在抽帧，跳过本次")
            return False
        try:
            return self._refresh_latest_from_ring_unlocked()
        finally:
            lock.release()

    def _refresh_latest_from_ring_unlocked(self) -> bool:
        """
        从抓包 TS 环形缓冲落盘，再用 GStreamer tsdemux 抽一帧。
        监测 FFmpeg 不参与出图。
        """
        if not self._capture_key:
            return False
        try:
            from iface_mcast import snapshot_ts, align_ts_sync
        except ImportError:
            try:
                from workers.iface_mcast import snapshot_ts, align_ts_sync
            except ImportError:
                return False
        try:
            iface, group, port, _cid = self._capture_key
        except Exception:
            return False
        data = snapshot_ts(iface, group, port, min_bytes=800 * 1024)
        if not data:
            return False
        data = align_ts_sync(data)
        # 按整路码率留约 8 秒，让后面还有完整关键帧。不解完整 64MB。
        try:
            from iface_mcast import hub_stats
        except ImportError:
            try:
                from workers.iface_mcast import hub_stats
            except ImportError:
                hub_stats = None
        bitrate = 0.0
        if hub_stats is not None:
            try:
                bitrate = float((hub_stats(iface, group, port) or {}).get("bitrate_kbps") or 0)
            except (TypeError, ValueError):
                bitrate = 0.0
        max_grab = thumb_tail_nbytes(bitrate)
        if len(data) > max_grab:
            data = align_ts_sync(data[-max_grab:])
        self.snapshot_dir.mkdir(parents=True, exist_ok=True)
        ts_path = self.snapshot_dir / (".ring_%s.ts" % self.id)
        jpg_tmp = self.snapshot_dir / (".latest_%s.tmp.jpg" % self.id)
        try:
            with open(str(ts_path), "wb") as f:
                f.write(data)
                f.flush()
        except OSError as e:
            self.logger.warning("写 TS ring 失败: %s" % e)
            return False

        map_list = thumb_video_maps(self.program)

        grab_prefix = ".gf_%s_" % self.id

        def _clear_grab_frames():
            try:
                for old in self.snapshot_dir.glob(grab_prefix + "*.jpg"):
                    try:
                        old.unlink()
                    except OSError:
                        pass
            except OSError:
                pass

        def _displayable(blob):
            try:
                from frame_quality import jpeg_looks_displayable
            except ImportError:
                try:
                    from workers.frame_quality import jpeg_looks_displayable
                except ImportError:
                    return True, ""
            return jpeg_looks_displayable(blob)

        def _run(maps, skip_key):
            # 开头那张常是半截。连抽几张关键帧，用靠后的一张已经见过参数集的。
            n_frames = 6 if skip_key else 1
            _clear_grab_frames()
            pattern = str(self.snapshot_dir / (grab_prefix + "%02d.jpg"))
            cmd = [
                "ffmpeg",
                "-y",
                "-hide_banner",
                "-loglevel",
                "error",
                "-fflags",
                "+genpts+igndts",
                "-err_detect",
                "ignore_err",
                "-probesize",
                "8M",
                "-analyzeduration",
                "5M",
            ]
            if skip_key:
                cmd.extend(["-skip_frame", "nokey"])
            cmd.extend(["-f", "mpegts", "-i", str(ts_path)])
            cmd.extend(list(maps))
            cmd.extend(
                [
                    "-an",
                    "-frames:v",
                    str(n_frames),
                    "-q:v",
                    "5",
                    "-start_number",
                    "1",
                    pattern,
                ]
            )
            try:
                r = subprocess.run(
                    cmd,
                    timeout=18,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                )
            except subprocess.TimeoutExpired:
                _clear_grab_frames()
                return False, "timeout", b""
            err = (r.stderr or b"").decode("utf-8", "replace").strip()
            last = err.splitlines()[-1][:200] if err else ""
            paths = []
            try:
                paths = sorted(self.snapshot_dir.glob(grab_prefix + "*.jpg"))
            except OSError:
                paths = []
            chosen = b""
            tear = b""
            reason = last
            try:
                for path in reversed(paths):
                    try:
                        blob = path.read_bytes()
                    except OSError:
                        continue
                    if len(blob) <= 1024:
                        continue
                    good, why = _displayable(blob)
                    if good:
                        chosen = blob
                        break
                    reason = why or reason
                    # 竖彩条是截图切坏的，不当花屏。绿条仍留给检测。
                    if why in ("conceal", "green") and blob and not tear:
                        tear = blob
            finally:
                _clear_grab_frames()
            if not chosen:
                return False, reason or last, tear
            try:
                jpg_tmp.write_bytes(chosen)
            except OSError:
                return False, "write", b""
            return True, last, b""

        def _map_label(maps):
            for item in maps or []:
                if isinstance(item, str) and item.startswith("0:"):
                    return item
            return ""

        def _try_maps(skip_key):
            found_ok = False
            found_err = ""
            found_tear = b""
            saw_program = False
            for maps in map_list:
                # 这一套已经解出竖条时，0:v:0 会抽成旁边的节目，停在这里。
                if skip_other_program_map(maps, saw_program):
                    break
                good, err, blob = _run(maps, skip_key)
                if good:
                    return True, err, b"", _map_label(maps)
                found_err = err
                if err in ("conceal", "green") and blob:
                    found_tear = blob
                if map_is_program(maps) and err in _FRAME_REJECT_REASONS:
                    saw_program = True
            return found_ok, found_err, found_tear, ""

        last_err = ""
        ok = False
        via = "gst"
        tear = b""
        try:
            # 先抽关键帧。参考帧丢了才会被涂成绿条，关键帧本身通常还是正常画面。
            # 关键帧不行再抽任意帧。两张都不正常，才把绿图留给花屏检测。
            # 节目号对上但解不出流时，才试 0:v:1。已经解出废图就不再换节目。
            via = "ffmpeg_key"
            ok, last_err, tear, map_used = _try_maps(True)
            if map_used:
                via = "ffmpeg_key/" + map_used
            if not ok:
                via = "ffmpeg"
                ok, err2, tear2, map_used = _try_maps(False)
                if ok:
                    tear = b""
                    if map_used:
                        via = "ffmpeg/" + map_used
                else:
                    last_err = err2 or last_err
                    if tear2:
                        tear = tear2
            if not ok:
                via = "gst"
                ok, last_err = self._gst_grab_jpeg(ts_path, jpg_tmp)
            if ok:
                try:
                    blob = jpg_tmp.read_bytes()
                except OSError:
                    blob = b""
                os.replace(str(jpg_tmp), str(self.latest_frame_path))
                jpg_tmp = None
                try:
                    (self.snapshot_dir / "latest_ok.jpg").write_bytes(blob)
                except OSError:
                    pass
                self._latest_valid = True
                self._note_media()
                now = time.time()
                last = getattr(self, "_last_gst_ok_log", 0.0)
                if now - last >= 30:
                    self.logger.info(
                        "[thumb] latest_ok size=%d via=%s ring=%dKB"
                        % (len(blob), via, int(len(data) / 1024))
                    )
                    self._last_gst_ok_log = now
                return True
            if tear:
                self._stash_ai_frame(tear)
            self.logger.warning(
                "[thumb] ring_grab fail ring=%dKB program=%s %s"
                % (int(len(data) / 1024), self.program, last_err or "no frame")
            )
            try:
                dbg = self.snapshot_dir / "debug_ring.ts"
                if (not dbg.is_file()) or (
                    time.time() - dbg.stat().st_mtime > 60
                ):
                    os.replace(str(ts_path), str(dbg))
                    ts_path = None
            except OSError:
                pass
            return False
        finally:
            for p in (ts_path, jpg_tmp):
                if p is None:
                    continue
                try:
                    if p.is_file():
                        p.unlink()
                except OSError:
                    pass

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
            "+genpts+discardcorrupt+igndts+nobuffer",
            "-flags",
            "low_delay",
            "-err_detect",
            "ignore_err",
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
            cmd.extend(["-map", "0:p:%d:v" % int(self.program)])
        else:
            cmd.extend(["-map", "0:v:0"])
        cmd.extend(
            [
                "-an",
                "-vf",
                "scale=640:-2",
                "-c:v",
                "mjpeg",
                "-q:v",
                "5",
                "-f",
                "image2",
                "-update",
                "1",
                "-flush_packets",
                "1",
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

    def _run_gst_live_thumb(self) -> None:
        """
        抓包 hub 持续喂 tsdemux（先灌 ring 再跟直播包）。
        有限 ring 文件抽不出 GOP；直播喂入才能等到关键帧。
        """
        gst = shutil.which("gst-launch-1.0") or "/usr/bin/gst-launch-1.0"
        if not os.path.isfile(gst) or not self._capture_key:
            return
        try:
            from iface_mcast import (
                TsFeeder,
                align_ts_sync,
                register_feeder,
                snapshot_ts,
                unregister_feeder,
            )
        except ImportError:
            try:
                from workers.iface_mcast import (
                    TsFeeder,
                    align_ts_sync,
                    register_feeder,
                    snapshot_ts,
                    unregister_feeder,
                )
            except ImportError:
                return
        iface, group, port, cid = self._capture_key
        feeder_id = "%s-gst" % cid
        feeder = TsFeeder()
        if register_feeder(iface, group, port, feeder_id, feeder) is None:
            return
        self.snapshot_dir.mkdir(parents=True, exist_ok=True)
        out = str(self.latest_frame_path)
        err_path = self.snapshot_dir / "thumb_gst.err"
        cmd = [
            gst,
            "-q",
            "fdsrc",
            "fd=0",
            "!",
            "queue",
            "leaky=downstream",
            "max-size-bytes=4194304",
            "max-size-buffers=0",
            "max-size-time=0",
            "!",
            "tsdemux",
        ]
        if self.program is not None:
            cmd.append("program-number=%d" % int(self.program))
        cmd.extend(
            [
            "!",
            "h264parse",
            "!",
            "avdec_h264",
            "!",
            "videoconvert",
            "!",
            "videoscale",
            "!",
            "video/x-raw,width=640,height=360",
            "!",
            "jpegenc",
            "quality=80",
            "!",
            "multifilesink",
            "location=%s" % out,
            "max-files=1",
            ]
        )
        err_f = open(str(err_path), "w")
        env = os.environ.copy()
        env["GST_DEBUG"] = "0"
        self.logger.info(
            "[thumb] start gst_live program=%s -> %s" % (self.program, out)
        )
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=err_f,
            env=env,
        )
        self._thumb_proc = proc
        self._thumb_feeder = feeder
        logged = False
        last_mtime = 0.0
        try:
            if proc.stdin is not None:
                try:
                    fcntl.fcntl(
                        proc.stdin.fileno(),
                        getattr(fcntl, "F_SETPIPE_SZ", 1031),
                        1 << 20,
                    )
                except (OSError, ValueError, AttributeError):
                    pass
            # 不灌 ring：短窗口 CC 缺口会让 tsdemux 卡在 preroll。
            # 直播包持续写入，等完整 GOP（与监测 FFmpeg 一样）。
            while (
                self.running
                and self._state in ("running", "starting")
                and proc.poll() is None
            ):
                batch = feeder.get_batch(timeout=0.5)
                if batch:
                    try:
                        proc.stdin.write(batch)
                    except Exception:
                        break
                try:
                    if self.latest_frame_path.is_file():
                        st = self.latest_frame_path.stat()
                        if st.st_size > 2048 and st.st_mtime != last_mtime:
                            last_mtime = st.st_mtime
                            blob = self.latest_frame_path.read_bytes()
                            if self._promote_jpeg_bytes(blob) and not logged:
                                self.logger.info(
                                    "[thumb] latest_ok size=%d via=gst_live"
                                    % len(blob)
                                )
                                logged = True
                except OSError:
                    pass
        finally:
            try:
                if proc.stdin:
                    proc.stdin.close()
            except Exception:
                pass
            unregister_feeder(iface, group, port, feeder_id)
            self._thumb_feeder = None
            if proc.poll() is None:
                try:
                    proc.terminate()
                    proc.wait(timeout=3)
                except Exception:
                    try:
                        proc.kill()
                    except Exception:
                        pass
            self._thumb_proc = None
            try:
                err_f.close()
            except Exception:
                pass
            if not logged:
                try:
                    tail = err_path.read_text()[-240:]
                except Exception:
                    tail = ""
                self.logger.warning(
                    "[thumb] gst_live end rc=%s %s"
                    % (proc.poll(), tail.replace("\n", " ")[:200])
                )

    def _promote_jpeg_bytes(self, data):
        if not data or not self._is_complete_jpeg(data):
            return False
        try:
            from frame_quality import jpeg_looks_displayable
        except ImportError:
            try:
                from workers.frame_quality import jpeg_looks_displayable
            except ImportError:
                jpeg_looks_displayable = None
        if jpeg_looks_displayable is not None:
            good, reason = jpeg_looks_displayable(data)
            if not good:
                self._stash_rejected_thumb(data, reason)
                now = time.time()
                last = getattr(self, "_last_bad_thumb_log", 0.0)
                if now - last >= 30:
                    self.logger.info(
                        "[thumb] skip_%s size=%d (keep last good)"
                        % (reason, len(data))
                    )
                    self._last_bad_thumb_log = now
                return False
        ok_path = self.snapshot_dir / "latest_ok.jpg"
        tmp = self.snapshot_dir / (".ok_%s.tmp.jpg" % self.id)
        try:
            with self._latest_lock:
                tmp.write_bytes(data)
                os.replace(str(tmp), str(ok_path))
        except OSError:
            return False
        self._latest_valid = True
        self._note_media()
        return True

    def _start_thumb_thread(self):
        """
        截图：iface 抓包喂 GStreamer tsdemux；无 iface 则独立 ffmpeg 抽一帧。
        """

        if self.frame_interval_sec <= 0:
            return
        if self._thumb_thread and self._thumb_thread.is_alive():
            return

        def _loop():
            self.snapshot_dir.mkdir(parents=True, exist_ok=True)
            fail_streak = 0
            stagger = (hash(self.id) % 17) * 0.25
            if stagger > 0:
                end = time.time() + stagger
                while self.running and time.time() < end:
                    time.sleep(min(0.3, max(0.05, end - time.time())))
            self.logger.info(
                "[thumb] thread_run mode=gst_ring -> %s" % self.latest_frame_path
            )
            while self.running:
                if self._state not in ("running", "starting"):
                    self._stop_thumb_proc()
                    time.sleep(1.0)
                    continue
                try:
                    ok_path = self.snapshot_dir / "latest_ok.jpg"
                    fresh = False
                    try:
                        if (
                            ok_path.is_file()
                            and ok_path.stat().st_size > 2048
                            and (_now_ts() - ok_path.stat().st_mtime) < 15
                        ):
                            fresh = True
                    except OSError:
                        fresh = False
                    if fresh:
                        pass
                    elif self._capture_key:
                        self._refresh_latest_from_ring()
                    else:
                        self._grab_frame_ffmpeg(self.latest_frame_path)
                except Exception as e:
                    self.logger.warning("实时截图刷新异常: %s" % e)
                    fail_streak += 1
                have_ok = False
                try:
                    have_ok = (
                        (self.snapshot_dir / "latest_ok.jpg").is_file()
                        and (self.snapshot_dir / "latest_ok.jpg").stat().st_size
                        > 2048
                    )
                except OSError:
                    have_ok = False
                interval = 8.0 if not have_ok else 20.0
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
        """保留每个频道最近 N 张告警 jpg。不删 latest.jpg / latest_ok.jpg。"""
        try:
            files = sorted(
                (
                    p
                    for p in self.snapshot_dir.glob("*.jpg")
                    if _is_alarm_snapshot_name(p.name)
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

    def _write_latest_jpeg(self, data: bytes) -> None:
        if not data or len(data) < 1024 or not self._is_complete_jpeg(data):
            return
        try:
            from frame_quality import jpeg_looks_displayable
        except ImportError:
            try:
                from workers.frame_quality import jpeg_looks_displayable
            except ImportError:
                jpeg_looks_displayable = None
        if jpeg_looks_displayable is not None:
            ok, reason = jpeg_looks_displayable(data)
            if not ok:
                self._stash_rejected_thumb(data, reason)
                now = time.time()
                last = getattr(self, "_last_bad_thumb_log", 0.0)
                if now - last >= 30:
                    self.logger.info("[thumb] skip_%s size=%d (keep last good)" % (reason, len(data)))
                    self._last_bad_thumb_log = now
                return
        self.snapshot_dir.mkdir(parents=True, exist_ok=True)
        tmp = self.snapshot_dir / (".pipe_%s.tmp.jpg" % self.id)
        ok_path = self.snapshot_dir / "latest_ok.jpg"
        try:
            with self._latest_lock:
                tmp.write_bytes(data)
                os.replace(str(tmp), str(self.latest_frame_path))
                try:
                    ok_path.write_bytes(data)
                except OSError:
                    pass
        except OSError as e:
            self.logger.debug("写 latest.jpg 失败: %s" % e)
            try:
                if tmp.is_file():
                    tmp.unlink()
            except OSError:
                pass
            return
        self._latest_valid = True
        self._note_media()

    def _watch_latest_file(self, proc: subprocess.Popen) -> None:
        """FFmpeg 直接覆盖 latest.jpg；此处只做绿/灰过滤并复制 latest_ok.jpg。"""
        path = self.latest_frame_path
        ok_path = self.snapshot_dir / "latest_ok.jpg"
        last_mtime = 0.0
        logged = False
        self.logger.info("[thumb] jpg watch started -> %s" % path)
        while self.running and proc.poll() is None:
            try:
                if path.is_file():
                    st = path.stat()
                    if st.st_size > 1024 and st.st_mtime != last_mtime:
                        last_mtime = st.st_mtime
                        try:
                            with self._latest_lock:
                                data = path.read_bytes()
                        except OSError:
                            data = b""
                        if not self._is_complete_jpeg(data):
                            continue
                        try:
                            from frame_quality import jpeg_looks_displayable
                        except ImportError:
                            try:
                                from workers.frame_quality import jpeg_looks_displayable
                            except ImportError:
                                jpeg_looks_displayable = None
                        if jpeg_looks_displayable is not None:
                            good, reason = jpeg_looks_displayable(data)
                            if not good:
                                self._stash_rejected_thumb(data, reason)
                                now = time.time()
                                last = getattr(self, "_last_bad_thumb_log", 0.0)
                                if now - last >= 30:
                                    self.logger.info(
                                        "[thumb] skip_%s size=%d (keep last good)"
                                        % (reason, len(data))
                                    )
                                    self._last_bad_thumb_log = now
                                continue
                        try:
                            with self._latest_lock:
                                tmp = self.snapshot_dir / (".ok_%s.tmp.jpg" % self.id)
                                tmp.write_bytes(data)
                                os.replace(str(tmp), str(ok_path))
                        except OSError:
                            continue
                        self._latest_valid = True
                        self._note_media()
                        if not logged:
                            self.logger.info(
                                "[thumb] latest_ok size=%d via=image2" % len(data)
                            )
                            logged = True
            except OSError:
                pass
            time.sleep(0.5)
        if not logged:
            self.logger.info("[thumb] jpg watch ended without frame")

    def _ensure_snap_fifo(self) -> Path:
        import stat as _stat

        self.snapshot_dir.mkdir(parents=True, exist_ok=True)
        p = self.snapshot_dir / "live.rgb"
        try:
            if p.exists() and not _stat.S_ISFIFO(p.stat().st_mode):
                p.unlink()
        except OSError:
            pass
        if not p.exists():
            os.mkfifo(str(p), 0o600)
        return p

    def _drain_raw_frames(self, proc: subprocess.Popen, stdout=None) -> None:
        """读固定尺寸 RGB24 帧，转 JPEG 后做绿/灰过滤再写入 latest.jpg。"""
        width, height = 320, 180
        frame_len = width * height * 3
        interval = max(float(self.frame_interval_sec), 1.0)
        if stdout is None:
            stdout = proc.stdout
        if stdout is None:
            self.logger.warning("[thumb] raw fifo is None")
            return
        try:
            from frame_quality import rgb_to_jpeg
        except ImportError:
            from workers.frame_quality import rgb_to_jpeg

        buf = bytearray()
        got = 0
        frames = 0
        logged = False
        last_save = 0.0
        last_dbg = time.time()
        try:
            fd = stdout.fileno()
        except Exception:
            fd = None
        self.logger.info("[thumb] raw drain started %dx%d" % (width, height))
        try:
            while self.running and proc.poll() is None:
                try:
                    ready, _, _ = select.select([stdout], [], [], 2.0)
                except (ValueError, OSError):
                    break
                if not ready:
                    now = time.time()
                    if now - last_dbg >= 15 and not logged:
                        self.logger.info(
                            "[thumb] raw_pipe bytes=%d frames=%d buf=%d (idle)"
                            % (got, frames, len(buf))
                        )
                        last_dbg = now
                    continue
                try:
                    if fd is not None:
                        chunk = os.read(fd, 65536)
                    else:
                        chunk = stdout.read(65536)
                except OSError:
                    chunk = b""
                if not chunk:
                    if proc.poll() is not None:
                        break
                    continue
                got += len(chunk)
                buf.extend(chunk)
                while len(buf) >= frame_len:
                    raw = bytes(buf[:frame_len])
                    del buf[:frame_len]
                    frames += 1
                    now = time.time()
                    if now - last_save < interval:
                        continue
                    jpeg = rgb_to_jpeg(raw, width, height, quality=80)
                    if not jpeg:
                        continue
                    self._write_latest_jpeg(jpeg)
                    last_save = now
                    if not logged:
                        self.logger.info(
                            "[thumb] latest_ok size=%d via=raw_pipe" % len(jpeg)
                        )
                        logged = True
                now = time.time()
                if now - last_dbg >= 15 and not logged:
                    self.logger.info(
                        "[thumb] raw_pipe bytes=%d frames=%d buf=%d"
                        % (got, frames, len(buf))
                    )
                    last_dbg = now
        except Exception as e:
            self.logger.debug("raw 管道结束: %s" % e)
        try:
            stdout.close()
        except Exception:
            pass

    def _copy_latest_frame(self, dest: Path) -> bool:
        """从旁路 latest.jpg 安全复制到 dest（完整 JPEG 才写入）。"""
        data = self._read_latest_jpeg_bytes()
        if not data:
            return False
        try:
            from frame_quality import jpeg_looks_displayable
        except ImportError:
            try:
                from workers.frame_quality import jpeg_looks_displayable
            except ImportError:
                jpeg_looks_displayable = None
        if jpeg_looks_displayable is not None:
            good, _reason = jpeg_looks_displayable(data)
            if not good:
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

    def _jpeg_ok_for_alarm(self, data: bytes) -> bool:
        if not data or len(data) < 2048 or not self._is_complete_jpeg(data):
            return False
        try:
            from frame_quality import jpeg_looks_displayable
        except ImportError:
            try:
                from workers.frame_quality import jpeg_looks_displayable
            except ImportError:
                jpeg_looks_displayable = None
        if jpeg_looks_displayable is None:
            return True
        good, _reason = jpeg_looks_displayable(data)
        return bool(good)

    def _grab_alarm_from_ring(self, anomaly_age_sec: float) -> Optional[bytes]:
        """抽当前异常画面。网卡收流时只读收包缓冲尾部，不再另开 UDP。"""
        if not self._capture_key:
            return None
        lock = getattr(self, "_ring_grab_lock", None)
        if lock is None:
            lock = threading.Lock()
            self._ring_grab_lock = lock
        if not lock.acquire(timeout=20):
            self.logger.warning("告警截图跳过: 收包缓冲正在抽帧")
            return None
        try:
            return self._grab_alarm_from_ring_unlocked(anomaly_age_sec)
        finally:
            lock.release()

    def _grab_alarm_from_ring_unlocked(self, anomaly_age_sec: float) -> Optional[bytes]:
        try:
            from iface_mcast import align_ts_sync, hub_stats, snapshot_ts
        except ImportError:
            try:
                from workers.iface_mcast import align_ts_sync, hub_stats, snapshot_ts
            except ImportError:
                return None
        try:
            iface, group, port, _cid = self._capture_key
        except Exception:
            return None
        data = snapshot_ts(iface, group, port, min_bytes=160 * 1024)
        if not data:
            self.logger.warning("告警截图失败: 收包缓冲还不够")
            return None
        data = align_ts_sync(data)
        if len(data) > 12 * 1024 * 1024:
            data = align_ts_sync(data[-12 * 1024 * 1024 :])
        try:
            bitrate = float((hub_stats(iface, group, port) or {}).get("bitrate_kbps") or 0)
        except (TypeError, ValueError):
            bitrate = 0.0

        self.snapshot_dir.mkdir(parents=True, exist_ok=True)
        ts_path = self.snapshot_dir / (".alarm_%s.ts" % self.id)
        jpg_tmp = self.snapshot_dir / (".alarm_%s.tmp.jpg" % self.id)
        last_err = ""
        wrote = False
        seen_len = set()
        try:
            for span in alarm_tail_spans(anomaly_age_sec):
                chunk = align_ts_sync(alarm_ring_tail(data, bitrate, span))
                if len(chunk) < 32 * 1024 or len(chunk) in seen_len:
                    continue
                seen_len.add(len(chunk))
                try:
                    with open(str(ts_path), "wb") as f:
                        f.write(chunk)
                        f.flush()
                except OSError as e:
                    self.logger.warning("告警截图写缓冲失败: %s" % e)
                    return None
                wrote = True
                blob, last_err = self._ffmpeg_alarm_jpeg(ts_path, jpg_tmp)
                if blob:
                    self.logger.info(
                        "告警截图取自缓冲尾部 age=%.1fs bitrate=%.0fkbps span=%.1fs bytes=%d"
                        % (float(anomaly_age_sec or 0), bitrate, span, len(chunk))
                    )
                    return blob
            if wrote:
                ok, last_err = self._gst_grab_jpeg(ts_path, jpg_tmp)
                if ok:
                    try:
                        blob = jpg_tmp.read_bytes()
                    except OSError:
                        blob = b""
                    if self._jpeg_ok_for_alarm(blob):
                        self.logger.info(
                            "告警截图取自缓冲尾部 age=%.1fs bitrate=%.0fkbps via=gst"
                            % (float(anomaly_age_sec or 0), bitrate)
                        )
                        return blob
            self.logger.warning(
                "告警截图失败: 缓冲尾部没有可显示画面 %s" % (last_err or "")
            )
            return None
        finally:
            for p in (ts_path, jpg_tmp):
                try:
                    if p.is_file():
                        p.unlink()
                except OSError:
                    pass

    def _ffmpeg_alarm_jpeg(self, ts_path: Path, jpg_tmp: Path):
        """从已经截短的 TS 里解一帧。文件本身就是异常时段的尾部。"""
        map_list = thumb_video_maps(self.program)
        last = ""
        saw_program = False
        for maps in map_list:
            if skip_other_program_map(maps, saw_program):
                break
            try:
                if jpg_tmp.is_file():
                    jpg_tmp.unlink()
            except OSError:
                pass
            cmd = [
                "ffmpeg",
                "-y",
                "-hide_banner",
                "-loglevel",
                "error",
                "-fflags",
                "+genpts+igndts",
                "-err_detect",
                "ignore_err",
                "-probesize",
                "2M",
                "-analyzeduration",
                "1M",
                "-f",
                "mpegts",
                "-i",
                str(ts_path),
            ]
            cmd.extend(list(maps))
            cmd.extend(["-an", "-frames:v", "1", "-q:v", "3", str(jpg_tmp)])
            try:
                r = subprocess.run(
                    cmd,
                    timeout=8,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                )
            except subprocess.TimeoutExpired:
                last = "timeout"
                continue
            err = (r.stderr or b"").decode("utf-8", "replace").strip()
            last = err.splitlines()[-1][:200] if err else ""
            if not (jpg_tmp.is_file() and jpg_tmp.stat().st_size > 2048):
                continue
            try:
                blob = jpg_tmp.read_bytes()
            except OSError:
                continue
            if self._jpeg_ok_for_alarm(blob):
                return blob, last
            if map_is_program(maps):
                saw_program = True
            last = last or "not_displayable"
        return None, last

    def _store_alarm_jpeg(self, dest: Path, data: bytes) -> bool:
        tmp = dest.with_suffix(dest.suffix + ".part")
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_bytes(data)
            os.replace(str(tmp), str(dest))
            return dest.is_file() and dest.stat().st_size >= 2048
        except OSError as e:
            self.logger.warning("写入告警截图失败: %s" % e)
            try:
                if tmp.is_file():
                    tmp.unlink()
            except OSError:
                pass
            return False

    def _take_snapshot(self, event_type: str, started: Optional[float] = None) -> Optional[Path]:
        """
        黑场/静帧留下当时的异常画面。
        大屏合格图可能是异常开始前的正常节目，不能拿来当告警截图。
        网卡收流时从收包缓冲尾部抽一帧，不再另开一路 UDP。
        """
        if event_type in ("stream_down",) or str(event_type).endswith("_end"):
            return None
        visual = event_type in ("black", "freeze") or str(event_type).startswith("ai_")
        if not visual:
            return None

        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        out_path = self.snapshot_dir / f"{event_type}_{ts}.jpg"
        age = 0.0
        if started:
            try:
                age = max(0.0, _now_ts() - float(started))
            except (TypeError, ValueError):
                age = 0.0

        def _bg():
            with self._snapshot_lock:
                if self._snapshot_inflight:
                    return
                self._snapshot_inflight = True
            try:
                if self._capture_key:
                    blob = self._grab_alarm_from_ring(age)
                    if blob and self._store_alarm_jpeg(out_path, blob):
                        self.logger.info("截图已保存(异常画面): %s" % out_path)
                        self._prune_snapshots()
                        return
                    self.logger.warning("截图失败: %s" % out_path)
                    return
                if self._grab_frame_ffmpeg(out_path, quality=3, keyframe_only=True):
                    try:
                        if out_path.stat().st_size < 2048:
                            out_path.unlink()
                            self.logger.warning("截图过小已丢弃（可能花屏）")
                            return
                    except OSError:
                        pass
                    self.logger.info("截图已保存(关键帧): %s" % out_path)
                    self._prune_snapshots()
                else:
                    self.logger.warning("截图失败: %s" % out_path)
            finally:
                with self._snapshot_lock:
                    self._snapshot_inflight = False

        threading.Thread(
            target=_bg, name=f"snap-{self.id}-{event_type}", daemon=True
        ).start()
        return None

    # ---------- FFmpeg 行解析 ----------

    _RE_PEAK = re.compile(
        r"lavfi\.astats\.Overall\.Peak_level=(-?\d+(?:\.\d+)?|-?inf|nan)",
        re.IGNORECASE,
    )
    _RE_DURATION = re.compile(
        r"(?:black_duration|freeze_duration|silence_duration)\s*[:=]\s*([0-9.]+)",
        re.I,
    )

    def _commit_alarm_start(self, alarm_key: str, event: Dict, started: Optional[float] = None):
        """真正落库的开始告警（已过确认期）。"""
        now = _now_ts()
        cool = self._cooldown_until.get(alarm_key, 0)
        if now < cool:
            self.logger.info(
                "告警冷却中，忽略 %s（还需 %.0fs）"
                % (alarm_key, cool - now)
            )
            return
        self._active_alarms[alarm_key] = started if started else now
        self._cooldown_until[alarm_key] = now + self.alarm_cooldown_sec
        if self.save_snapshot:
            snap = self._take_snapshot(event["type"], started=started)
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
            need = self._alarm_need_sec(key)
            if key == "freeze":
                if (
                    self._run_started_ts
                    and now - self._run_started_ts < self.freeze_startup_ignore_sec
                ):
                    continue
            if now - item["since"] >= need:
                if key == "freeze" and not self._freeze_silence_ready(now, need):
                    if not item.get("silence_hold_logged"):
                        item["silence_hold_logged"] = True
                        self.logger.info("静帧待确认，伴音仍在，不落账")
                    continue
                if (
                    key == "freeze"
                    and self.freeze_mode == "video_silence"
                    and not self._audio_unavailable
                ):
                    item["event"]["message"] = "检测到静帧无伴音"
                self._commit_alarm_start(key, item["event"], started=item["since"])
                done.append(key)
        for key in done:
            self._pending_alarms.pop(key, None)

    def _freeze_silence_ready(self, now: float, need: float) -> bool:
        """静帧且无伴音：画面已满时长，伴音也连续低于门限满同一时长。无音频则只看画面。"""
        if self.freeze_mode != "video_silence" or self._audio_unavailable:
            return True
        if not self._silence_active or self._silence_since is None:
            return False
        return (now - float(self._silence_since)) >= float(need)

    def _note_silence_line(self, lower: str) -> None:
        if "silence_start" in lower:
            self._silence_active = True
            self._silence_since = _now_ts() - float(self._silence_probe_d or 0)
        elif "silence_end" in lower:
            self._silence_active = False
            self._silence_since = None

    def _note_audio_missing(self, lower: str) -> None:
        if self._audio_unavailable:
            return
        if "matches no streams" in lower or (
            "stream specifier" in lower and ":a" in lower
        ):
            self._audio_unavailable = True
            self._audio_db = None
            self._audio_db_ts = 0.0
            self.logger.warning("未找到音频，下一轮静帧只看画面")
            self._write_status()

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
            if alarm_key == "freeze" and self._run_started_ts:
                if _now_ts() - self._run_started_ts < self.freeze_startup_ignore_sec:
                    return
            if alarm_key in self._active_alarms:
                return
            if alarm_key not in self._pending_alarms:
                lead = self._detector_lead_sec(alarm_key)
                self._pending_alarms[alarm_key] = {
                    "event": event,
                    "since": _now_ts() - lead,
                }
                wait_s = self._alarm_need_sec(alarm_key)
                self.logger.info(
                    "待确认告警 %s（满 %.1fs 才记）" % (alarm_key, wait_s)
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
            wall = round(_now_ts() - start_ts, 3) if start_ts else None
            if wall is not None:
                # 静帧以墙钟为准；FFmpeg freeze_duration 仍可能是错误 PTS
                if alarm_key == "freeze" or "duration" not in event:
                    event["duration"] = wall
            dur = event.get("duration")
            if (
                alarm_key == "freeze"
                and wall is not None
                and wall < float(self.freeze_duration)
            ):
                event["message"] = "静帧误报已撤销（持续 %.1fs，不足 %.0fs）" % (
                    wall,
                    float(self.freeze_duration),
                )
                self.logger.info(json.dumps(event, ensure_ascii=False))
                self._write_status()
                return
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
        if self._note_peak_line(line):
            return
        now = _now_str()
        lower = line.lower()
        self._note_audio_missing(lower)
        if "silence_start" in lower or "silence_end" in lower:
            self._note_silence_line(lower)
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

    def _stash_rejected_thumb(self, data: bytes, reason: str) -> None:
        """整幅花屏留给检测。带画面的纯绿横条是解码补块，不送去报花屏。"""
        if reason == "conceal":
            return
        self._stash_ai_frame(data)

    def _stash_ai_frame(self, data: bytes) -> None:
        """大屏不显示的完整画面留给花屏检测。不改 latest.jpg / latest_ok.jpg。"""
        if not data or len(data) <= 2048 or not self._is_complete_jpeg(data):
            return
        path = self.snapshot_dir / "latest_ai.jpg"
        tmp = self.snapshot_dir / (".ai_keep_%s.tmp.jpg" % self.id)
        try:
            self.snapshot_dir.mkdir(parents=True, exist_ok=True)
            tmp.write_bytes(data)
            os.replace(str(tmp), str(path))
        except OSError:
            try:
                if tmp.is_file():
                    tmp.unlink()
            except OSError:
                pass

    def _ai_may_grab_udp(self) -> bool:
        """网卡抓包已经占住这一路组播。AI 不能再开第二个 UDP。"""
        return not self._capture_key

    def _jpeg_ident(self, path: Path):
        try:
            st = path.stat()
        except OSError:
            return None
        mtime = getattr(st, "st_mtime_ns", None)
        if mtime is None:
            mtime = st.st_mtime
        return (path.name, mtime, st.st_size)

    def _ai_frame_candidates(self) -> List[Path]:
        """已有画面里较新的一张。纯绿花屏也要留下，不能按缩略图标准丢掉。"""
        now = _now_ts()
        found = []
        for name in ("latest.jpg", "latest_ok.jpg", "latest_ai.jpg"):
            path = self.snapshot_dir / name
            try:
                st = path.stat()
            except OSError:
                continue
            if st.st_size <= 2048:
                continue
            if now - st.st_mtime > 45:
                continue
            found.append((st.st_mtime, path))
        found.sort(key=lambda item: item[0], reverse=True)
        return [path for _, path in found]

    def _read_jpeg_file(self, path: Path, max_age: float = 45.0) -> Optional[bytes]:
        """读完整 JPEG。不判断好不好看，花屏本身就要送去检测。"""
        for attempt in range(3):
            try:
                with self._latest_lock:
                    if not path.is_file():
                        return None
                    st = path.stat()
                    if st.st_size <= 2048:
                        return None
                    if _now_ts() - st.st_mtime > max_age:
                        return None
                    data = path.read_bytes()
                if len(data) > 2048 and self._is_complete_jpeg(data):
                    return data
            except OSError:
                return None
            time.sleep(0.02 * (attempt + 1))
        return None

    @staticmethod
    def _ai_alarm_text(label: str) -> str:
        if label == "green_screen+mosaic":
            return "检测到花屏马赛克"
        if label == "green_screen":
            return "检测到花屏"
        if label == "mosaic":
            return "检测到马赛克"
        return "检测到花屏或马赛克"

    @staticmethod
    def _ai_alarm_type(label: str) -> str:
        if label in ("green_screen", "green_screen+mosaic"):
            return "ai_green_screen"
        return "ai_mosaic"

    def _analyze_frame_for_ai(self, frame_path: Path) -> bool:
        """分析一帧。读不到图返回 False，不因此清掉连续计数。"""
        if not self.ai or not self.ai.is_ready:
            return False
        work_path = frame_path
        tmp_copy: Optional[Path] = None
        try:
            if frame_path.name in ("latest.jpg", "latest_ok.jpg", "latest_ai.jpg"):
                data = self._read_jpeg_file(frame_path, 45.0)
                if not data:
                    return False
                tmp_copy = self.snapshot_dir / (
                    ".ai_read_%s_%s.jpg" % (os.getpid(), threading.get_ident())
                )
                tmp_copy.write_bytes(data)
                work_path = tmp_copy
            elif not frame_path.is_file() or frame_path.stat().st_size <= 2048:
                return False
            result = self.ai.analyze_image(str(work_path))
            label = str(result.get("label") or "")
            if label in ("skipped", "unreadable", "error", ""):
                return True
            now = _now_ts()
            if not result.get("is_anomaly"):
                self._ai_bad_since = None
                return True
            if now < float(self._ai_cool_until or 0):
                return True
            if self._ai_bad_since is None:
                self._ai_bad_since = now
                return True
            confirm = float(getattr(self.ai, "confirm_sec", 6.0) or 6.0)
            if confirm < 1.0:
                confirm = 1.0
            if now - float(self._ai_bad_since) < confirm:
                return True
            safe = label.replace("+", "_").replace("/", "_")
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            archive = self.snapshot_dir / ("ai_%s_%s.jpg" % (safe, ts))
            try:
                if work_path.is_file():
                    shutil.copy2(work_path, archive)
                else:
                    archive = work_path
            except OSError:
                archive = work_path
            event = {
                "type": self._ai_alarm_type(label),
                "phase": "start",
                "channel_id": self.id,
                "channel_name": self.name,
                "message": self._ai_alarm_text(label),
                "score": result.get("score"),
                "detail": result.get("detail"),
                "backend": result.get("backend"),
                "time": _now_str(),
                "snapshot": str(archive),
            }
            self._ai_cool_until = now + float(self.alarm_cooldown_sec)
            self._ai_bad_since = None
            self.logger.warning(json.dumps(event, ensure_ascii=False))
            self._save_event(event)
            self._prune_snapshots()
            return True
        except Exception as e:
            self.logger.debug("AI 分析跳过: %s" % e)
            return False
        finally:
            if tmp_copy is not None:
                try:
                    tmp_copy.unlink(missing_ok=True)
                except OSError:
                    pass

    def _run_ai_once(self) -> bool:
        cands = self._ai_frame_candidates()
        if cands:
            top = cands[0]
            ident = self._jpeg_ident(top)
            # 同一张图反复读不算时间过去，避免一张坏图停在磁盘上就报花屏。
            if ident is not None and ident == self._ai_last_ident:
                return True
            if self._analyze_frame_for_ai(top):
                if ident is not None:
                    self._ai_last_ident = ident
                return True
            for cand in cands[1:]:
                ident = self._jpeg_ident(cand)
                if ident is not None and ident == self._ai_last_ident:
                    continue
                if self._analyze_frame_for_ai(cand):
                    if ident is not None:
                        self._ai_last_ident = ident
                    return True
            return False
        if not self._ai_may_grab_udp():
            return False
        tmp = self.snapshot_dir / "ai_frame_tmp.jpg"
        if not self._grab_frame_ffmpeg(tmp, quality=4):
            return False
        try:
            return self._analyze_frame_for_ai(tmp)
        finally:
            try:
                if tmp.is_file():
                    tmp.unlink(missing_ok=True)
            except OSError:
                pass

    def _ai_loop(self):
        """独立线程：读已有画面。网卡抓包时不再另开 UDP。"""
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

                if self._run_ai_once():
                    self._last_ai_ts = now
                    time.sleep(0.1)
                else:
                    time.sleep(0.5)
            except Exception as e:
                self.logger.debug("AI 线程异常: %s" % e)
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

        def _bg():
            if self._run_ai_once():
                self._last_ai_ts = time.time()

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
        # 管道不是 TTY 时 glibc 会块缓冲 stderr，Stream 信息要等退出才刷出。
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
        self._latest_valid = False
        self._silence_active = False
        self._silence_since = None
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
            if "Overall.Peak_level" not in line and "Parsed_ametadata" not in line:
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
