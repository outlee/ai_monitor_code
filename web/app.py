#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AI 节目监测 - Web 前端服务

面板功能：
- 频道状态 / 事件 / 截图
- 功能开关：AI 启停、检测模式、截图开关、各频道启用
- 无登录鉴权（内网使用）

配置写入 channels.yaml；Manager/Worker 默认热重载（约数秒内生效）。

启动：
  cd ai_monitor
  pip3 install fastapi uvicorn pyyaml
  python3 -m uvicorn web.app:app --host 0.0.0.0 --port 8080
"""

from __future__ import annotations

import json
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml
from fastapi import FastAPI, HTTPException, Query, UploadFile, File
from fastapi.responses import FileResponse, HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

CONFIG_PATH = ROOT / "config" / "channels.yaml"
LOG_DIR = ROOT / "logs"
STATUS_DIR = LOG_DIR / "status"
EVENTS_FILE = LOG_DIR / "events.jsonl"
SNAPSHOT_DIR = ROOT / "snapshots"
STATIC_DIR = Path(__file__).resolve().parent / "static"

_config_lock = threading.Lock()

try:
    import event_db
except Exception:
    event_db = None  # type: ignore

app = FastAPI(title="AI 节目监测", version="0.5.0")

if event_db is not None:
    try:
        event_db.configure(ROOT)
    except Exception:
        pass

if STATIC_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# ---------- 配置读写 ----------

def _load_config() -> Dict[str, Any]:
    if not CONFIG_PATH.is_file():
        return {"defaults": {}, "ai": {}, "channels": []}
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _save_config(cfg: Dict[str, Any]) -> None:
    """写回 YAML，保留基本可读结构。"""
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _config_lock:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            yaml.safe_dump(
                cfg,
                f,
                allow_unicode=True,
                default_flow_style=False,
                sort_keys=False,
            )


def _read_events(limit: int = 100, channel_id: Optional[str] = None) -> List[Dict]:
    if not EVENTS_FILE.is_file():
        return []
    try:
        with open(EVENTS_FILE, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        return []

    events: List[Dict] = []
    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if channel_id and ev.get("channel_id") != channel_id:
            continue
        events.append(ev)
        if len(events) >= limit:
            break
    return events


def _list_snapshots(channel_id: Optional[str] = None, limit: int = 40) -> List[Dict]:
    items: List[Dict] = []
    if not SNAPSHOT_DIR.is_dir():
        return items

    if channel_id:
        dirs = [SNAPSHOT_DIR / channel_id] if (SNAPSHOT_DIR / channel_id).is_dir() else []
    else:
        dirs = [p for p in SNAPSHOT_DIR.iterdir() if p.is_dir()]

    for d in dirs:
        for f in d.glob("*.jpg"):
            try:
                st = f.stat()
                items.append(
                    {
                        "channel_id": d.name,
                        "filename": f.name,
                        "url": f"/api/snapshots/{d.name}/{f.name}",
                        "size": st.st_size,
                        "mtime": datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
                        "mtime_ts": st.st_mtime,
                    }
                )
            except OSError:
                continue

    items.sort(key=lambda x: x["mtime_ts"], reverse=True)
    return items[:limit]


def _read_status_file(channel_id: str) -> Optional[Dict[str, Any]]:
    path = STATUS_DIR / f"{channel_id}.json"
    if not path.is_file():
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def _is_alarm_event_type(t: Optional[str]) -> bool:
    """开始类异常（不含 *_end 与纯信息事件）。"""
    if not t:
        return False
    if t.endswith("_end"):
        return False
    if t in ("stream_down",):
        return True
    if t.startswith("ai_"):
        return True
    return t in ("black", "freeze", "silence")


def _channel_stats(
    channels: List[Dict],
    events: List[Dict],
    stale_sec: float = 30.0,
) -> List[Dict]:
    """
    状态优先级：
    disabled > offline/stale > reconnecting > alarm > ok > unknown
    心跳来自 logs/status/<id>.json（Worker 写入）。
    """
    now = time.time()
    by_ch: Dict[str, Dict] = {}
    for ch in channels:
        cid = ch.get("id", "")
        by_ch[cid] = {
            "id": cid,
            "name": ch.get("name", cid),
            "url": ch.get("url", ""),
            "program": ch.get("program"),
            "enabled": ch.get("enabled", True),
            "event_count": 0,
            "last_event": None,
            "last_type": None,
            "status": "unknown",
            "heartbeat": None,
            "state": None,
            "active_alarms": [],
            "reconnect_count": 0,
        }

    for ev in events:
        cid = ev.get("channel_id")
        if not cid or cid not in by_ch:
            if len(channels) == 1 and "channel_id" not in ev:
                cid = channels[0]["id"]
            else:
                continue
        info = by_ch[cid]
        info["event_count"] += 1
        if info["last_event"] is None:
            info["last_event"] = ev.get("time")
            info["last_type"] = ev.get("type")

    for info in by_ch.values():
        if not info["enabled"]:
            info["status"] = "disabled"
            continue

        st = _read_status_file(info["id"])
        if st:
            info["heartbeat"] = st.get("last_heartbeat")
            info["state"] = st.get("state")
            info["active_alarms"] = st.get("active_alarms") or []
            info["reconnect_count"] = st.get("reconnect_count") or 0
            hb_ts = st.get("last_heartbeat_ts")
            try:
                hb_ts_f = float(hb_ts) if hb_ts is not None else 0.0
            except (TypeError, ValueError):
                hb_ts_f = 0.0
            age = now - hb_ts_f if hb_ts_f else 99999.0
            worker_state = (st.get("state") or "").lower()

            if age > stale_sec:
                info["status"] = "stale"
            elif worker_state == "reconnecting":
                info["status"] = "reconnecting"
            elif worker_state in ("stopped", "disabled"):
                info["status"] = "offline"
            elif info["active_alarms"]:
                info["status"] = "alarm"
            elif worker_state in ("running", "starting"):
                # 最近事件是开始类告警且尚未被 end 清掉 active 时已覆盖；
                # 无 active 则视为正常
                info["status"] = "ok"
            else:
                info["status"] = "unknown"
        else:
            # 无心跳：若有最近告警事件仍标 alarm，否则 offline
            if _is_alarm_event_type(info.get("last_type")):
                info["status"] = "offline"
            else:
                info["status"] = "offline"

    return list(by_ch.values())


# ---------- 请求体 ----------

class AIUpdate(BaseModel):
    enabled: Optional[bool] = None
    mode: Optional[str] = None
    interval_sec: Optional[float] = Field(None, ge=0.5, le=60)
    threshold: Optional[float] = Field(None, ge=0.0, le=1.0)
    green_ratio_th: Optional[float] = Field(None, ge=0.0, le=1.0)
    block_score_th: Optional[float] = Field(None, ge=0.0, le=1.0)


class DefaultsUpdate(BaseModel):
    save_snapshot: Optional[bool] = None
    black_duration: Optional[float] = Field(None, ge=0.5, le=60)
    freeze_duration: Optional[float] = Field(None, ge=0.5, le=60)
    silence_duration: Optional[float] = Field(None, ge=0.5, le=60)
    silence_threshold: Optional[float] = Field(None, ge=-80, le=0)


class ChannelUpdate(BaseModel):
    enabled: Optional[bool] = None
    name: Optional[str] = None
    url: Optional[str] = None
    # None=不修改；传 null 可清空（见 model_fields_set）
    program: Optional[int] = Field(None, ge=0, le=65535)


class ChannelCreate(BaseModel):
    id: str
    name: str
    url: str
    enabled: bool = True
    program: Optional[int] = Field(None, ge=0, le=65535)


class ChannelImport(BaseModel):
    """导入频道列表。mode=replace 全量替换；mode=merge 按 id 合并（同 id 覆盖）。"""
    mode: str = "merge"  # merge | replace
    channels: List[Dict[str, Any]]


# ---------- 页面与只读 API ----------

@app.get("/", response_class=HTMLResponse)
def index():
    index_file = STATIC_DIR / "index.html"
    if not index_file.is_file():
        return HTMLResponse("<h1>前端文件缺失</h1>", status_code=500)
    return HTMLResponse(index_file.read_text(encoding="utf-8"))


@app.get("/api/overview")
def api_overview():
    cfg = _load_config()
    channels = cfg.get("channels") or []
    ai = cfg.get("ai") or {}
    defaults = cfg.get("defaults") or {}
    stale_sec = float(defaults.get("status_stale_sec", 30.0))
    events = _read_events(limit=200)
    stats = _channel_stats(channels, events, stale_sec=stale_sec)
    alarm_count = sum(1 for s in stats if s["status"] == "alarm")
    offline_count = sum(
        1 for s in stats if s["status"] in ("offline", "stale", "reconnecting")
    )
    enabled_count = sum(1 for c in channels if c.get("enabled", True))

    return {
        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "channel_total": len(channels),
        "channel_enabled": enabled_count,
        "channel_alarm": alarm_count,
        "channel_offline": offline_count,
        "event_total_loaded": len(events),
        "ai": {
            "enabled": bool(ai.get("enabled", False)),
            "mode": ai.get("mode", "auto"),
            "model_path": ai.get("model_path", "models/mosaic_detector.onnx"),
            "interval_sec": float(ai.get("interval_sec", 2.0)),
            "threshold": float(ai.get("threshold", 0.55)),
            "green_ratio_th": float(ai.get("green_ratio_th", 0.35)),
            "block_score_th": float(ai.get("block_score_th", 0.12)),
        },
        "defaults": {
            "save_snapshot": bool(defaults.get("save_snapshot", True)),
            "black_duration": float(defaults.get("black_duration", 2.0)),
            "freeze_duration": float(defaults.get("freeze_duration", 3.0)),
            "silence_duration": float(defaults.get("silence_duration", 3.0)),
            "silence_threshold": defaults.get("silence_threshold", -40),
            "status_stale_sec": stale_sec,
        },
        "channels": stats,
        "recent_events": events[:50],
        "note": "配置已支持热重载：Manager/Worker 运行中修改将在数秒内自动生效（可用 --no-reload 关闭）",
    }


@app.get("/api/status/{channel_id}")
def api_channel_status(channel_id: str):
    st = _read_status_file(channel_id)
    if not st:
        raise HTTPException(404, f"无心跳: {channel_id}")
    return st


@app.get("/api/channels")
def api_channels():
    cfg = _load_config()
    return {
        "channels": cfg.get("channels") or [],
        "ai": cfg.get("ai") or {},
        "defaults": cfg.get("defaults") or {},
    }


@app.get("/api/events")
def api_events(
    limit: int = Query(50, ge=1, le=500),
    channel_id: Optional[str] = None,
):
    return {"events": _read_events(limit=limit, channel_id=channel_id)}


@app.get("/api/snapshots")
def api_snapshots(
    limit: int = Query(40, ge=1, le=200),
    channel_id: Optional[str] = None,
):
    return {"snapshots": _list_snapshots(channel_id=channel_id, limit=limit)}


@app.get("/api/snapshots/{channel_id}/{filename}")
def api_snapshot_file(channel_id: str, filename: str):
    if ".." in channel_id or ".." in filename or "/" in filename or "\\" in filename:
        raise HTTPException(400, "非法路径")
    path = SNAPSHOT_DIR / channel_id / filename
    if not path.is_file():
        raise HTTPException(404, "截图不存在")
    return FileResponse(path, media_type="image/jpeg")


@app.get("/api/health")
def api_health():
    status_count = 0
    if STATUS_DIR.is_dir():
        status_count = len(list(STATUS_DIR.glob("*.json")))
    info = {
        "ok": True,
        "root": str(ROOT),
        "config_exists": CONFIG_PATH.is_file(),
        "events_exists": EVENTS_FILE.is_file(),
        "status_files": status_count,
        "sqlite": None,
    }
    if event_db is not None:
        try:
            info["sqlite"] = event_db.storage_info()
        except Exception as e:
            info["sqlite"] = {"error": str(e)}
    return info


@app.get("/api/dashboard")
def api_dashboard():
    """大屏数据：频道交通灯 + 24h 统计。"""
    cfg = _load_config()
    channels = cfg.get("channels") or []
    defaults = cfg.get("defaults") or {}
    stale_sec = float(defaults.get("status_stale_sec", 30.0))
    events = _read_events(limit=100)
    stats = _channel_stats(channels, events, stale_sec=stale_sec)

    # 四色灯：ok / alarm / offline|stale|reconnecting / disabled
    def lamp(st: str) -> str:
        if st == "ok":
            return "green"
        if st == "alarm":
            return "red"
        if st in ("offline", "stale", "reconnecting"):
            return "yellow"
        if st == "disabled":
            return "gray"
        return "gray"

    cards = []
    for s in stats:
        cards.append(
            {
                "id": s["id"],
                "name": s["name"],
                "status": s["status"],
                "lamp": lamp(s["status"]),
                "last_type": s.get("last_type"),
                "active_alarms": s.get("active_alarms") or [],
                "program": s.get("program"),
                "enabled": s.get("enabled", True),
            }
        )

    hist = None
    if event_db is not None:
        try:
            hist = event_db.alert_stats_24h()
        except Exception:
            hist = None

    return {
        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "cards": cards,
        "summary": {
            "total": len(cards),
            "green": sum(1 for c in cards if c["lamp"] == "green"),
            "red": sum(1 for c in cards if c["lamp"] == "red"),
            "yellow": sum(1 for c in cards if c["lamp"] == "yellow"),
            "gray": sum(1 for c in cards if c["lamp"] == "gray"),
        },
        "stats_24h": hist,
        "recent_events": events[:30],
    }


@app.get("/api/alerts/history")
def api_alerts_history(
    limit: int = Query(100, ge=1, le=2000),
    channel_id: Optional[str] = None,
    event_type: Optional[str] = None,
    hours: Optional[float] = Query(None, ge=0.1, le=720),
):
    """SQLite 告警历史；库不可用时回落 events.jsonl。"""
    since_ts = None
    if hours is not None:
        since_ts = time.time() - float(hours) * 3600
    if event_db is not None:
        try:
            rows = event_db.query_alerts(
                limit=limit,
                channel_id=channel_id,
                event_type=event_type,
                since_ts=since_ts,
            )
            return {"source": "sqlite", "alerts": rows}
        except Exception as e:
            pass
    # fallback
    evs = _read_events(limit=limit, channel_id=channel_id)
    if event_type:
        evs = [e for e in evs if e.get("type") == event_type]
    return {"source": "jsonl", "alerts": evs}


@app.get("/api/storage")
def api_storage():
    if event_db is None:
        return {"ok": False, "message": "event_db 不可用"}
    return {"ok": True, **event_db.storage_info()}


# ---------- 写配置 API（无鉴权）----------

@app.post("/api/config/ai")
def api_update_ai(body: AIUpdate):
    cfg = _load_config()
    ai = cfg.setdefault("ai", {})
    changed = []

    if body.enabled is not None:
        ai["enabled"] = bool(body.enabled)
        changed.append(f"enabled={ai['enabled']}")
    if body.mode is not None:
        if body.mode not in ("auto", "onnx", "heuristic", "off"):
            raise HTTPException(400, "mode 必须是 auto|onnx|heuristic|off")
        ai["mode"] = body.mode
        changed.append(f"mode={body.mode}")
    if body.interval_sec is not None:
        ai["interval_sec"] = float(body.interval_sec)
        changed.append(f"interval_sec={body.interval_sec}")
    if body.threshold is not None:
        ai["threshold"] = float(body.threshold)
        changed.append(f"threshold={body.threshold}")
    if body.green_ratio_th is not None:
        ai["green_ratio_th"] = float(body.green_ratio_th)
        changed.append(f"green_ratio_th={body.green_ratio_th}")
    if body.block_score_th is not None:
        ai["block_score_th"] = float(body.block_score_th)
        changed.append(f"block_score_th={body.block_score_th}")

    if not changed:
        raise HTTPException(400, "没有可更新的字段")

    _save_config(cfg)
    return {
        "ok": True,
        "changed": changed,
        "ai": ai,
        "message": "AI 配置已保存，热重载后数秒内生效",
    }


@app.post("/api/config/defaults")
def api_update_defaults(body: DefaultsUpdate):
    cfg = _load_config()
    defaults = cfg.setdefault("defaults", {})
    changed = []

    if body.save_snapshot is not None:
        defaults["save_snapshot"] = bool(body.save_snapshot)
        changed.append(f"save_snapshot={defaults['save_snapshot']}")
    if body.black_duration is not None:
        defaults["black_duration"] = float(body.black_duration)
        changed.append(f"black_duration={body.black_duration}")
    if body.freeze_duration is not None:
        defaults["freeze_duration"] = float(body.freeze_duration)
        changed.append(f"freeze_duration={body.freeze_duration}")
    if body.silence_duration is not None:
        defaults["silence_duration"] = float(body.silence_duration)
        changed.append(f"silence_duration={body.silence_duration}")
    if body.silence_threshold is not None:
        defaults["silence_threshold"] = float(body.silence_threshold)
        changed.append(f"silence_threshold={body.silence_threshold}")

    if not changed:
        raise HTTPException(400, "没有可更新的字段")

    _save_config(cfg)
    return {
        "ok": True,
        "changed": changed,
        "defaults": defaults,
        "message": "默认检测参数已保存，热重载后数秒内生效",
    }


def _validate_channel_id(cid: str) -> str:
    cid = (cid or "").strip()
    if not cid:
        raise HTTPException(400, "频道 ID 不能为空")
    if any(c in cid for c in "/\\.. \t"):
        raise HTTPException(400, "频道 ID 含非法字符")
    if len(cid) > 64:
        raise HTTPException(400, "频道 ID 过长")
    return cid


def _parse_program_value(raw: Any) -> Optional[int]:
    """空字符串/None → 不设 program；否则解析为非负整数。"""
    if raw is None:
        return None
    if isinstance(raw, str) and not raw.strip():
        return None
    try:
        p = int(raw)
    except (TypeError, ValueError):
        raise HTTPException(400, "program 必须是整数（MPEG-TS program/service id）")
    if p < 0 or p > 65535:
        raise HTTPException(400, "program 范围 0~65535")
    return p


def _normalize_channel(raw: Dict[str, Any], require_all: bool = True) -> Dict[str, Any]:
    cid = _validate_channel_id(str(raw.get("id", "")))
    name = str(raw.get("name") or cid).strip()
    url = str(raw.get("url") or "").strip()
    if require_all and not url:
        raise HTTPException(400, f"频道 {cid} 缺少 url")
    if url and not (
        url.startswith("udp://")
        or url.startswith("rtp://")
        or url.startswith("rtsp://")
        or url.startswith("http://")
        or url.startswith("https://")
        or url.startswith("file:")
        or url.endswith(".ts")
        or url.endswith(".mp4")
    ):
        # 宽松提示，不强制拦截本地测试路径
        pass
    out: Dict[str, Any] = {
        "id": cid,
        "name": name or cid,
        "url": url,
        "enabled": bool(raw.get("enabled", True)),
    }
    # 仅当源数据带 program 键时写入（导入可带可不带）
    if "program" in raw:
        prog = _parse_program_value(raw.get("program"))
        if prog is not None:
            out["program"] = prog
    return out


@app.post("/api/config/channels")
def api_create_channel(body: ChannelCreate):
    cfg = _load_config()
    channels = cfg.get("channels") or []
    ch = _normalize_channel(body.model_dump(), require_all=True)
    if any(c.get("id") == ch["id"] for c in channels):
        raise HTTPException(400, f"频道 ID 已存在: {ch['id']}")
    channels.append(ch)
    cfg["channels"] = channels
    _save_config(cfg)
    return {
        "ok": True,
        "channel": ch,
        "message": "频道已添加，热重载后数秒内生效",
    }


@app.post("/api/config/channels/{channel_id}")
def api_update_channel(channel_id: str, body: ChannelUpdate):
    cfg = _load_config()
    channels = cfg.get("channels") or []
    target = None
    for ch in channels:
        if ch.get("id") == channel_id:
            target = ch
            break
    if target is None:
        raise HTTPException(404, f"频道不存在: {channel_id}")

    changed = []
    data = body.model_dump(exclude_unset=True)

    if "enabled" in data and data["enabled"] is not None:
        target["enabled"] = bool(data["enabled"])
        changed.append(f"enabled={target['enabled']}")
    if "name" in data and data["name"] is not None:
        name = str(data["name"]).strip()
        if not name:
            raise HTTPException(400, "名称不能为空")
        target["name"] = name
        changed.append(f"name={name}")
    if "url" in data and data["url"] is not None:
        url = str(data["url"]).strip()
        if not url:
            raise HTTPException(400, "地址不能为空")
        target["url"] = url
        changed.append("url")
    if "program" in data:
        # 显式传 program: null 或省略值 → 清除；传数字 → 设置
        prog = data["program"]
        if prog is None:
            if "program" in target:
                target.pop("program", None)
                changed.append("program=cleared")
        else:
            p = _parse_program_value(prog)
            if p is None:
                target.pop("program", None)
                changed.append("program=cleared")
            else:
                target["program"] = p
                changed.append(f"program={p}")

    if not changed:
        raise HTTPException(400, "没有可更新的字段")

    cfg["channels"] = channels
    _save_config(cfg)
    return {
        "ok": True,
        "channel_id": channel_id,
        "changed": changed,
        "channel": target,
        "message": "频道配置已保存，热重载后数秒内生效",
    }


@app.delete("/api/config/channels/{channel_id}")
def api_delete_channel(channel_id: str):
    cfg = _load_config()
    channels = cfg.get("channels") or []
    new_list = [c for c in channels if c.get("id") != channel_id]
    if len(new_list) == len(channels):
        raise HTTPException(404, f"频道不存在: {channel_id}")
    cfg["channels"] = new_list
    _save_config(cfg)
    return {
        "ok": True,
        "channel_id": channel_id,
        "message": f"已删除频道 {channel_id}，热重载后数秒内生效",
        "remaining": len(new_list),
    }


@app.get("/api/config/export")
def api_export_channels(fmt: str = Query("json", pattern="^(json|yaml)$")):
    """导出频道列表（不含 AI/defaults，便于迁移）。"""
    cfg = _load_config()
    channels = cfg.get("channels") or []
    payload = {"channels": channels, "exported_at": datetime.now().isoformat(timespec="seconds")}
    if fmt == "yaml":
        text = yaml.safe_dump(payload, allow_unicode=True, default_flow_style=False, sort_keys=False)
        return Response(
            content=text,
            media_type="application/x-yaml",
            headers={"Content-Disposition": "attachment; filename=channels_export.yaml"},
        )
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    return Response(
        content=text,
        media_type="application/json",
        headers={"Content-Disposition": "attachment; filename=channels_export.json"},
    )


@app.post("/api/config/import")
def api_import_channels(body: ChannelImport):
    if body.mode not in ("merge", "replace"):
        raise HTTPException(400, "mode 必须是 merge 或 replace")
    if not body.channels:
        raise HTTPException(400, "channels 不能为空")

    normalized = [_normalize_channel(c, require_all=True) for c in body.channels]
    # 导入列表内部 id 不能重复
    ids = [c["id"] for c in normalized]
    if len(ids) != len(set(ids)):
        raise HTTPException(400, "导入数据中存在重复频道 ID")

    cfg = _load_config()
    existing = cfg.get("channels") or []

    if body.mode == "replace":
        cfg["channels"] = normalized
        added, updated = len(normalized), 0
    else:
        by_id = {c.get("id"): dict(c) for c in existing}
        added = updated = 0
        for ch in normalized:
            if ch["id"] in by_id:
                by_id[ch["id"]].update(ch)
                updated += 1
            else:
                by_id[ch["id"]] = ch
                added += 1
        # 保持原有顺序，新频道追加
        order = [c.get("id") for c in existing if c.get("id") in by_id]
        for ch in normalized:
            if ch["id"] not in order:
                order.append(ch["id"])
        cfg["channels"] = [by_id[i] for i in order if i in by_id]

    _save_config(cfg)
    return {
        "ok": True,
        "mode": body.mode,
        "imported": len(normalized),
        "added": added if body.mode == "merge" else len(normalized),
        "updated": updated if body.mode == "merge" else 0,
        "total": len(cfg["channels"]),
        "message": "导入完成，热重载后数秒内生效",
    }


@app.post("/api/config/import/file")
async def api_import_file(
    file: UploadFile = File(...),
    mode: str = Query("merge", pattern="^(merge|replace)$"),
):
    """上传 JSON 或 YAML 文件导入频道。"""
    raw = await file.read()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise HTTPException(400, "文件编码必须是 UTF-8")

    name = (file.filename or "").lower()
    data: Any = None
    try:
        if name.endswith((".yaml", ".yml")):
            data = yaml.safe_load(text)
        else:
            data = json.loads(text)
    except Exception as e:
        raise HTTPException(400, f"解析失败: {e}")

    if isinstance(data, dict) and "channels" in data:
        channels = data["channels"]
    elif isinstance(data, list):
        channels = data
    else:
        raise HTTPException(400, "文件需包含 channels 数组，或直接为频道数组")

    if not isinstance(channels, list):
        raise HTTPException(400, "channels 必须是数组")

    return api_import_channels(ChannelImport(mode=mode, channels=channels))
