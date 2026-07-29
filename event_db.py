#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
轻量 SQLite 存储（借鉴 igmp_monitor 的持久化思路，不强制 Influx/Redis）

- 告警/事件历史（alerts）
- 频道状态快照采样（status_samples，可选）
- 与 events.jsonl 并存：Worker 双写；Web 可查询历史

默认库路径：<workdir>/data/monitor.db
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

_lock = threading.Lock()
_db_path: Optional[Path] = None


def _now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def configure(work_dir: str | Path) -> Path:
    """设置工作目录并初始化库，返回 db 路径。"""
    global _db_path
    root = Path(work_dir)
    data = root / "data"
    data.mkdir(parents=True, exist_ok=True)
    path = data / "monitor.db"
    _db_path = path
    init_db(path)
    return path


def get_db_path() -> Path:
    if _db_path is None:
        return configure(".")
    return _db_path


def _connect(path: Optional[Path] = None) -> sqlite3.Connection:
    p = path or get_db_path()
    conn = sqlite3.connect(str(p), timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def init_db(path: Optional[Path] = None) -> None:
    p = path or get_db_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    with _lock:
        conn = _connect(p)
        try:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS alerts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    time TEXT,
                    ts REAL,
                    channel_id TEXT,
                    channel_name TEXT,
                    type TEXT,
                    phase TEXT,
                    message TEXT,
                    score REAL,
                    payload TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_alerts_ts ON alerts(ts DESC);
                CREATE INDEX IF NOT EXISTS idx_alerts_ch ON alerts(channel_id, ts DESC);
                CREATE INDEX IF NOT EXISTS idx_alerts_type ON alerts(type);

                CREATE TABLE IF NOT EXISTS status_samples (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts REAL,
                    time TEXT,
                    channel_id TEXT,
                    state TEXT,
                    status_hint TEXT,
                    active_alarms TEXT,
                    reconnect_count INTEGER,
                    payload TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_status_ch_ts ON status_samples(channel_id, ts DESC);

                CREATE TABLE IF NOT EXISTS meta (
                    key TEXT PRIMARY KEY,
                    value TEXT
                );
                """
            )
            conn.commit()
        finally:
            conn.close()


def insert_alert(event: Dict[str, Any]) -> None:
    """写入一条告警/事件。"""
    path = get_db_path()
    ts = time.time()
    tstr = event.get("time") or _now_str()
    try:
        # 尝试解析 time 为 ts
        if event.get("time"):
            try:
                dt = datetime.strptime(str(event["time"]), "%Y-%m-%d %H:%M:%S")
                ts = dt.timestamp()
            except ValueError:
                pass
    except Exception:
        pass

    row = (
        tstr,
        ts,
        str(event.get("channel_id") or ""),
        str(event.get("channel_name") or ""),
        str(event.get("type") or ""),
        str(event.get("phase") or ""),
        str(event.get("message") or event.get("msg") or ""),
        float(event["score"]) if event.get("score") is not None else None,
        json.dumps(event, ensure_ascii=False),
    )
    with _lock:
        conn = _connect(path)
        try:
            conn.execute(
                """
                INSERT INTO alerts(time, ts, channel_id, channel_name, type, phase, message, score, payload)
                VALUES (?,?,?,?,?,?,?,?,?)
                """,
                row,
            )
            conn.commit()
        finally:
            conn.close()


def insert_status_sample(channel_id: str, payload: Dict[str, Any]) -> None:
    path = get_db_path()
    ts = float(payload.get("last_heartbeat_ts") or time.time())
    alarms = payload.get("active_alarms") or []
    if isinstance(alarms, list):
        alarms_s = json.dumps(alarms, ensure_ascii=False)
    else:
        alarms_s = str(alarms)
    with _lock:
        conn = _connect(path)
        try:
            conn.execute(
                """
                INSERT INTO status_samples(ts, time, channel_id, state, status_hint, active_alarms, reconnect_count, payload)
                VALUES (?,?,?,?,?,?,?,?)
                """,
                (
                    ts,
                    payload.get("last_heartbeat") or _now_str(),
                    channel_id,
                    payload.get("state"),
                    None,
                    alarms_s,
                    int(payload.get("reconnect_count") or 0),
                    json.dumps(payload, ensure_ascii=False),
                ),
            )
            conn.commit()
        finally:
            conn.close()


def query_alerts(
    limit: int = 100,
    channel_id: Optional[str] = None,
    event_type: Optional[str] = None,
    since_ts: Optional[float] = None,
) -> List[Dict[str, Any]]:
    path = get_db_path()
    sql = "SELECT id, time, ts, channel_id, channel_name, type, phase, message, score FROM alerts WHERE 1=1"
    params: List[Any] = []
    if channel_id:
        sql += " AND channel_id=?"
        params.append(channel_id)
    if event_type:
        sql += " AND type=?"
        params.append(event_type)
    if since_ts is not None:
        sql += " AND ts>=?"
        params.append(since_ts)
    sql += " ORDER BY ts DESC LIMIT ?"
    params.append(int(limit))

    with _lock:
        conn = _connect(path)
        try:
            cur = conn.execute(sql, params)
            rows = cur.fetchall()
        finally:
            conn.close()
    return [dict(r) for r in rows]


def alert_stats_24h() -> Dict[str, Any]:
    path = get_db_path()
    since = time.time() - 86400
    with _lock:
        conn = _connect(path)
        try:
            total = conn.execute(
                "SELECT COUNT(*) AS c FROM alerts WHERE ts>=?", (since,)
            ).fetchone()["c"]
            by_type = conn.execute(
                """
                SELECT type, COUNT(*) AS c FROM alerts
                WHERE ts>=? GROUP BY type ORDER BY c DESC LIMIT 20
                """,
                (since,),
            ).fetchall()
            by_ch = conn.execute(
                """
                SELECT channel_id, channel_name, COUNT(*) AS c FROM alerts
                WHERE ts>=? GROUP BY channel_id ORDER BY c DESC LIMIT 20
                """,
                (since,),
            ).fetchall()
        finally:
            conn.close()
    return {
        "hours": 24,
        "total": total,
        "by_type": [{"type": r["type"], "count": r["c"]} for r in by_type],
        "by_channel": [
            {
                "channel_id": r["channel_id"],
                "channel_name": r["channel_name"],
                "count": r["c"],
            }
            for r in by_ch
        ],
    }


def storage_info() -> Dict[str, Any]:
    path = get_db_path()
    size = path.stat().st_size if path.is_file() else 0
    with _lock:
        conn = _connect(path)
        try:
            n_alerts = conn.execute("SELECT COUNT(*) AS c FROM alerts").fetchone()["c"]
            n_status = conn.execute("SELECT COUNT(*) AS c FROM status_samples").fetchone()[
                "c"
            ]
        finally:
            conn.close()
    return {
        "db_path": str(path),
        "db_size_bytes": size,
        "alerts_count": n_alerts,
        "status_samples_count": n_status,
    }


def prune_old(days: int = 30) -> Dict[str, int]:
    """清理过期记录。"""
    path = get_db_path()
    cutoff = time.time() - days * 86400
    with _lock:
        conn = _connect(path)
        try:
            c1 = conn.execute("DELETE FROM alerts WHERE ts<?", (cutoff,)).rowcount
            c2 = conn.execute(
                "DELETE FROM status_samples WHERE ts<?", (cutoff,)
            ).rowcount
            conn.commit()
            conn.execute("VACUUM")
        finally:
            conn.close()
    return {"deleted_alerts": c1, "deleted_status_samples": c2}
