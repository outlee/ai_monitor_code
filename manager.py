#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AI 节目监测 Manager
- 按组启动多个 Worker 进程
- 日志写入文件（避免 PIPE 堵死）
- Worker 异常退出后自动拉活
- 配置热重载：监听 channels.yaml，按组指纹只重启变更的 Worker
适配 CentOS 环境
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, TextIO, Tuple

import yaml

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [Manager] %(levelname)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("manager")


def _stable_hash(obj: Any) -> str:
    raw = json.dumps(obj, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def group_fingerprint(
    group: List[Dict], defaults: Dict, ai: Dict
) -> str:
    """一组频道 + 影响监测的全局配置 → 指纹；变则需重启该 Worker。"""
    payload = {
        "channels": group,
        "defaults": defaults or {},
        "ai": ai or {},
    }
    return _stable_hash(payload)


@dataclass
class WorkerSlot:
    idx: int
    ids: List[str]
    cmd: List[str]
    log_path: Path
    fingerprint: str = ""
    proc: Optional[subprocess.Popen] = None
    log_fh: Optional[TextIO] = None
    restarts: int = 0
    consecutive_fails: int = 0
    last_start_ts: float = 0.0
    last_exit_ts: float = 0.0
    last_returncode: Optional[int] = None
    # 配置热重载主动停止，不计入崩溃拉活
    intentional_stop: bool = False
    # 配置重载导致的重启不累加 restarts 计数
    skip_restart_quota: bool = False


class MonitorManager:
    def __init__(
        self,
        config_path: str,
        work_dir: str,
        streams_per_worker: int = 6,
        max_restarts: int = 50,
        restart_delay: float = 3.0,
        restart_max_delay: float = 60.0,
        reload_interval: float = 3.0,
        enable_reload: bool = True,
    ):
        self.config_path = Path(config_path).resolve()
        self.work_dir = Path(work_dir).resolve()
        self.streams_per_worker = streams_per_worker
        self.max_restarts = max_restarts
        self.restart_delay = restart_delay
        self.restart_max_delay = restart_max_delay
        self.reload_interval = max(1.0, float(reload_interval))
        self.enable_reload = enable_reload
        self.workers: List[WorkerSlot] = []
        self.running = True

        self.config: Dict = {}
        self.channels: List[Dict] = []
        self.log_dir = self.work_dir / "logs"
        self._config_mtime: float = 0.0
        self._last_reload_check: float = 0.0
        self._reload_count: int = 0

        self._refresh_from_disk(initial=True)

    def _load_config(self) -> Dict:
        with open(self.config_path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}

    def _refresh_from_disk(self, initial: bool = False) -> None:
        self.config = self._load_config()
        self.channels = [
            ch for ch in (self.config.get("channels") or []) if ch.get("enabled", True)
        ]
        log_root = self.config.get("defaults", {}).get("log_dir", "logs")
        self.log_dir = self.work_dir / log_root
        self.log_dir.mkdir(parents=True, exist_ok=True)
        try:
            self._config_mtime = self.config_path.stat().st_mtime
        except OSError:
            self._config_mtime = 0.0
        if initial:
            logger.info(f"配置: {self.config_path} (mtime={self._config_mtime})")

    def _split_channels(self, channels: Optional[List[Dict]] = None) -> List[List[Dict]]:
        chs = channels if channels is not None else self.channels
        groups = []
        for i in range(0, len(chs), self.streams_per_worker):
            groups.append(chs[i : i + self.streams_per_worker])
        return groups

    def _build_cmd(self, ids: List[str]) -> List[str]:
        worker_script = self.work_dir / "workers" / "monitor_worker.py"
        cmd = [
            sys.executable,
            str(worker_script),
            "-c",
            str(self.config_path),
            "-w",
            str(self.work_dir),
            "--ids",
            *ids,
        ]
        if self.enable_reload:
            # Worker 内也热重载本进程频道；分组变化仍由 Manager 改 --ids
            cmd.extend(["--reload-interval", str(self.reload_interval)])
        else:
            cmd.append("--no-reload")
        return cmd

    def _desired_slots(self) -> List[Tuple[int, List[str], str, List[str]]]:
        defaults = self.config.get("defaults") or {}
        ai = self.config.get("ai") or {}
        groups = self._split_channels()
        out = []
        for idx, group in enumerate(groups):
            ids = [ch["id"] for ch in group]
            fp = group_fingerprint(group, defaults, ai)
            cmd = self._build_cmd(ids)
            out.append((idx, ids, fp, cmd))
        return out

    def _spawn(self, slot: WorkerSlot) -> None:
        if slot.log_fh:
            try:
                slot.log_fh.close()
            except Exception:
                pass
        slot.log_path.parent.mkdir(parents=True, exist_ok=True)
        slot.log_fh = open(slot.log_path, "a", encoding="utf-8", buffering=1)
        slot.log_fh.write(
            f"\n===== start {time.strftime('%Y-%m-%d %H:%M:%S')} "
            f"ids={slot.ids} fp={slot.fingerprint} =====\n"
        )
        slot.log_fh.flush()

        proc = subprocess.Popen(
            slot.cmd,
            cwd=str(self.work_dir),
            stdout=slot.log_fh,
            stderr=subprocess.STDOUT,
            universal_newlines=True,
        )
        slot.proc = proc
        slot.last_start_ts = time.time()
        slot.intentional_stop = False
        logger.info(
            f"启动 Worker-{slot.idx} pid={proc.pid} ids={slot.ids} "
            f"fp={slot.fingerprint} log={slot.log_path.name}"
        )

    def _stop_slot(self, slot: WorkerSlot, reason: str = "") -> None:
        slot.intentional_stop = True
        if slot.proc and slot.proc.poll() is None:
            logger.info(
                f"停止 Worker-{slot.idx} ids={slot.ids}"
                + (f" ({reason})" if reason else "")
            )
            try:
                slot.proc.terminate()
            except Exception:
                pass
            try:
                slot.proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                try:
                    slot.proc.kill()
                    slot.proc.wait(timeout=3)
                except Exception:
                    pass
        slot.proc = None
        if slot.log_fh:
            try:
                slot.log_fh.close()
            except Exception:
                pass
            slot.log_fh = None

    def start(self):
        desired = self._desired_slots()
        if not desired:
            logger.warning("没有启用的频道（将继续监听配置，待添加后自动启动）")
        else:
            logger.info(
                f"共 {len(self.channels)} 路节目，分成 {len(desired)} 个 Worker 组 "
                f"(每组最多 {self.streams_per_worker} 路)"
            )

        for idx, ids, fp, cmd in desired:
            slot = WorkerSlot(
                idx=idx,
                ids=ids,
                cmd=cmd,
                log_path=self.log_dir / f"worker-{idx}.log",
                fingerprint=fp,
            )
            self.workers.append(slot)
            self._spawn(slot)

        mode = "开启" if self.enable_reload else "关闭"
        logger.info(
            f"Worker 已就绪；配置热重载={mode}（间隔 {self.reload_interval}s）；"
            f"Ctrl+C 停止"
        )

        try:
            while self.running:
                time.sleep(1.0)
                self._supervise()
                if self.enable_reload:
                    self._maybe_reload_config()
        except KeyboardInterrupt:
            self.stop()

    def _maybe_reload_config(self) -> None:
        now = time.time()
        if now - self._last_reload_check < self.reload_interval:
            return
        self._last_reload_check = now

        try:
            mtime = self.config_path.stat().st_mtime
        except OSError as e:
            logger.warning(f"无法读取配置: {e}")
            return

        if mtime == self._config_mtime:
            return

        # 防抖：写入未完成时 mtime 可能连跳
        time.sleep(0.4)
        try:
            mtime2 = self.config_path.stat().st_mtime
        except OSError:
            return
        if mtime2 != mtime:
            return

        logger.info("检测到配置文件变更，开始热重载…")
        try:
            self._reload_config()
        except Exception as e:
            logger.error(f"热重载失败: {e}", exc_info=True)

    def _reload_config(self) -> None:
        old_mtime = self._config_mtime
        try:
            self._refresh_from_disk()
        except Exception as e:
            logger.error(f"加载配置失败，保持原 Worker: {e}")
            self._config_mtime = old_mtime
            return

        desired = self._desired_slots()
        desired_by_idx = {d[0]: d for d in desired}
        current_by_idx = {s.idx: s for s in self.workers}

        all_idx = sorted(set(desired_by_idx) | set(current_by_idx))
        started = stopped = restarted = kept = 0

        new_workers: List[WorkerSlot] = []
        for idx in all_idx:
            want = desired_by_idx.get(idx)
            have = current_by_idx.get(idx)

            if want is None and have is not None:
                self._stop_slot(have, reason="分组已移除")
                stopped += 1
                continue

            assert want is not None
            _idx, ids, fp, cmd = want

            if have is None:
                slot = WorkerSlot(
                    idx=idx,
                    ids=ids,
                    cmd=cmd,
                    log_path=self.log_dir / f"worker-{idx}.log",
                    fingerprint=fp,
                )
                self._spawn(slot)
                new_workers.append(slot)
                started += 1
                continue

            # 已有 slot
            if have.fingerprint == fp and have.ids == ids and have.proc and have.proc.poll() is None:
                # 指纹未变且进程存活：保留（cmd 路径可能相同）
                have.cmd = cmd
                new_workers.append(have)
                kept += 1
                continue

            # 需重启
            self._stop_slot(
                have,
                reason=f"配置变更 ids={ids} fp {have.fingerprint}->{fp}",
            )
            have.ids = ids
            have.cmd = cmd
            have.fingerprint = fp
            have.log_path = self.log_dir / f"worker-{idx}.log"
            have.skip_restart_quota = True
            have.consecutive_fails = 0
            self._spawn(have)
            have.skip_restart_quota = False
            new_workers.append(have)
            restarted += 1

        self.workers = sorted(new_workers, key=lambda s: s.idx)
        self._reload_count += 1
        logger.info(
            f"热重载完成 #{self._reload_count}: "
            f"保留={kept} 重启={restarted} 新建={started} 停止={stopped} "
            f"启用频道={len(self.channels)} Worker={len(self.workers)}"
        )
        self._write_manager_status()

    def _write_manager_status(self) -> None:
        path = self.log_dir / "status" / "_manager.json"
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "time": time.strftime("%Y-%m-%d %H:%M:%S"),
                "config": str(self.config_path),
                "config_mtime": self._config_mtime,
                "reload_count": self._reload_count,
                "reload_enabled": self.enable_reload,
                "channels_enabled": len(self.channels),
                "workers": [
                    {
                        "idx": s.idx,
                        "ids": s.ids,
                        "fingerprint": s.fingerprint,
                        "pid": s.proc.pid if s.proc and s.proc.poll() is None else None,
                        "restarts": s.restarts,
                    }
                    for s in self.workers
                ],
            }
            tmp = path.with_suffix(".json.tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            tmp.replace(path)
        except OSError:
            pass

    def _supervise(self):
        for slot in list(self.workers):
            if not slot.proc:
                continue
            rc = slot.proc.poll()
            if rc is None:
                slot.consecutive_fails = 0
                continue

            slot.last_returncode = rc
            slot.last_exit_ts = time.time()

            if slot.intentional_stop:
                slot.proc = None
                continue

            logger.error(
                f"Worker-{slot.idx} 已退出，返回码: {rc}，ids={slot.ids}"
            )

            if not self.running:
                return

            if slot.restarts >= self.max_restarts:
                logger.error(
                    f"Worker-{slot.idx} 重启次数已达上限 {self.max_restarts}，不再拉活"
                )
                slot.proc = None
                continue

            slot.consecutive_fails += 1
            delay = min(
                self.restart_delay * (1.5 ** (slot.consecutive_fails - 1)),
                self.restart_max_delay,
            )
            ran = slot.last_exit_ts - slot.last_start_ts
            if ran > 120:
                slot.consecutive_fails = 1
                delay = self.restart_delay

            logger.info(
                f"Worker-{slot.idx} 将在 {delay:.1f}s 后拉活 "
                f"(第 {slot.restarts + 1}/{self.max_restarts} 次)"
            )
            end = time.time() + delay
            while self.running and time.time() < end:
                time.sleep(0.5)
            if not self.running:
                return

            # 拉活前若配置已变，用最新 cmd
            if self.enable_reload:
                try:
                    self._refresh_from_disk()
                    for d in self._desired_slots():
                        if d[0] == slot.idx:
                            slot.ids, slot.fingerprint, slot.cmd = d[1], d[2], d[3]
                            break
                    else:
                        # 该组已不存在
                        logger.info(f"Worker-{slot.idx} 对应分组已不存在，不拉活")
                        slot.proc = None
                        self.workers = [s for s in self.workers if s.idx != slot.idx]
                        continue
                except Exception as e:
                    logger.warning(f"拉活前刷新配置失败: {e}")

            slot.restarts += 1
            try:
                self._spawn(slot)
            except Exception as e:
                logger.error(f"Worker-{slot.idx} 拉活失败: {e}")

    def stop(self):
        self.running = False
        logger.info("正在停止所有 Worker...")
        for slot in self.workers:
            self._stop_slot(slot, reason="Manager 退出")
        logger.info("全部停止完成")


def main():
    parser = argparse.ArgumentParser(description="AI 节目监测 Manager")
    parser.add_argument(
        "-c", "--config", default="config/channels.yaml", help="配置文件路径"
    )
    parser.add_argument("-w", "--workdir", default=".", help="工作目录")
    parser.add_argument(
        "-n",
        "--per-worker",
        type=int,
        default=4,
        help="每个 Worker 负责的路数（CPU 环境建议 3~6）",
    )
    parser.add_argument(
        "--max-restarts",
        type=int,
        default=50,
        help="单个 Worker 最大崩溃拉活次数",
    )
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

    manager = MonitorManager(
        args.config,
        args.workdir,
        args.per_worker,
        max_restarts=args.max_restarts,
        reload_interval=args.reload_interval,
        enable_reload=not args.no_reload,
    )

    def signal_handler(sig, frame):
        manager.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    manager.start()


if __name__ == "__main__":
    main()
