# -*- coding: utf-8 -*-
"""本机汇总服务。只监听 127.0.0.1，向三台监测机要数据。"""
from __future__ import annotations

import json
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode

from fastapi import FastAPI, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles

def _frozen() -> bool:
    return bool(getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"))


def _bundle_root() -> Path:
    """Readonly resource root: repo root in dev, _MEIPASS when frozen."""
    if _frozen():
        return Path(getattr(sys, "_MEIPASS"))
    return Path(__file__).resolve().parent.parent


def _writable_dir() -> Path:
    """Writable config dir: desktop/ in dev, exe folder when frozen."""
    if _frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


BUNDLE_ROOT = _bundle_root()
WRITE_ROOT = _writable_dir()
if not _frozen() and str(BUNDLE_ROOT) not in sys.path:
    sys.path.insert(0, str(BUNDLE_ROOT))

from desktop import hub_client, player  # noqa: E402
from web.channel_order import sort_cards_by_order  # noqa: E402
from web.hub import assemble_hub, assemble_perf  # noqa: E402
from web.snapshot_names import is_alarm_snapshot  # noqa: E402

HERE = WRITE_ROOT
NODES_PATH = WRITE_ROOT / "nodes.json"
ORDER_PATH = WRITE_ROOT / "data" / "channel_order.json"
STATIC_DIR = BUNDLE_ROOT / "web" / "static"
DESKTOP_STATIC = BUNDLE_ROOT / "desktop" / "static"
if not DESKTOP_STATIC.is_dir():
    DESKTOP_STATIC = WRITE_ROOT / "static"
INDEX_FILE = STATIC_DIR / "index.html"

app = FastAPI(title="AI Program Monitor Desktop", docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
app.mount("/desktop", StaticFiles(directory=str(DESKTOP_STATIC)), name="desktop-static")


def _nodes() -> List[Dict[str, str]]:
    try:
        return hub_client.load_nodes(NODES_PATH)
    except Exception as exc:
        raise HTTPException(500, "节点配置打不开: %s" % exc)


def _scope_of(request: Request, explicit: str = "") -> str:
    if explicit:
        return explicit
    return str(request.query_params.get("scope") or "all")


def _targets(scope: str) -> List[Dict[str, str]]:
    try:
        return hub_client.pick_nodes(_nodes(), scope)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


def _one(scope: str) -> Dict[str, str]:
    nodes = _targets(scope)
    if len(nodes) != 1:
        raise HTTPException(400, "请先在配置页只选一台服务器")
    return nodes[0]


def _call(node: Dict[str, str], path: str, timeout: float = 6.0) -> Optional[Dict[str, Any]]:
    try:
        return hub_client.fetch_json(node["url"], path, timeout=timeout)
    except (HTTPError, URLError, TimeoutError, OSError, ValueError):
        return None


def _forward(node: Dict[str, str], method: str, path: str, payload: Any = None) -> Dict[str, Any]:
    try:
        if method == "DELETE":
            return hub_client.post_delete(node["url"], path)
        return hub_client.post_json(node["url"], path, payload if payload is not None else {})
    except Exception as exc:
        raise HTTPException(502, "%s: %s" % (node.get("name") or node["id"], exc))


def _fanout(scope: str, path: str, payload: Any) -> Dict[str, Any]:
    lines = []
    failed = 0
    for node in _targets(scope):
        try:
            data = hub_client.post_json(node["url"], path, payload)
            msg = str((data or {}).get("message") or "已保存")
            lines.append("%s %s" % (node["name"], msg))
        except Exception as exc:
            failed += 1
            lines.append("%s 失败: %s" % (node["name"], exc))
    if failed == len(lines):
        raise HTTPException(502, "；".join(lines))
    return {"ok": failed == 0, "message": "；".join(lines)}


def _settings_node(scope: str) -> Optional[Dict[str, str]]:
    nodes = _targets(scope) if scope != "all" else _nodes()
    if scope != "all":
        return nodes[0]
    for node in nodes:
        if _call(node, "/api/health", timeout=2.0):
            return node
    return nodes[0] if nodes else None


@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    text = INDEX_FILE.read_text(encoding="utf-8")
    needle = '<script src="/static/app.js'
    insert = (
        '<link rel="stylesheet" href="/desktop/desktop.css?v=20261015desk15" />\n'
        '  <script src="/desktop/desktop.js?v=20261015desk15"></script>\n  '
    )
    if needle not in text:
        raise HTTPException(500, "页面里没有找到脚本")
    text = text.replace(needle, insert + needle, 1)
    return HTMLResponse(text)


@app.get("/api/health")
def api_health() -> Dict[str, Any]:
    alerts = 0
    size = 0
    for node in _nodes():
        data = _call(node, "/api/health", timeout=2.0) or {}
        sql = data.get("sqlite") or {}
        try:
            alerts += int(sql.get("alerts_count") or 0)
            size += int(sql.get("db_size_bytes") or 0)
        except (TypeError, ValueError):
            pass
    return {
        "ok": True,
        "desktop": True,
        "sqlite": {"alerts_count": alerts, "db_size_bytes": size},
    }


@app.get("/api/desktop/info")
def api_desktop_info() -> Dict[str, Any]:
    found = player.player_command(HERE)
    return {
        "desktop": True,
        "player": bool(found),
        "player_name": Path(found[0]).name if found else "",
    }


@app.get("/api/hub/nodes")
def api_hub_nodes() -> Dict[str, Any]:
    nodes = _nodes()
    return {"nodes": nodes, "active": True, "path": "desktop/nodes.json"}


@app.post("/api/hub/nodes")
async def api_hub_nodes_save(request: Request) -> Dict[str, Any]:
    body = await request.json()
    try:
        nodes = hub_client.load_nodes_from_payload({"nodes": body.get("nodes") or []})
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    hub_client.save_nodes(NODES_PATH, nodes)
    return {"ok": True, "nodes": nodes, "active": True, "message": "汇总地址已记在这台电脑上"}


@app.get("/api/channels/order")
def api_get_order() -> Dict[str, Any]:
    return {"ids": hub_client.read_order(ORDER_PATH)}


@app.post("/api/channels/order")
async def api_save_order(request: Request) -> Dict[str, Any]:
    body = await request.json()
    ids = [str(x) for x in (body.get("ids") or []) if x]
    hub_client.write_order(ORDER_PATH, ids)
    return {"ok": True, "ids": ids, "message": "排列已保存"}


@app.get("/api/dashboard")
def api_dashboard() -> Dict[str, Any]:
    nodes = _nodes()
    cache: Dict[str, Any] = {}

    def pull(node: Dict[str, str]):
        try:
            return node["url"], hub_client.fetch_json(
                node["url"], "/api/dashboard?local=1", timeout=6.0
            )
        except Exception:
            return node["url"], None

    with ThreadPoolExecutor(max_workers=max(1, len(nodes))) as pool:
        for url, data in pool.map(pull, nodes):
            cache[url] = data

    def fetch_json(url: str, path: str):
        if path == "/api/dashboard":
            data = cache.get(url)
            if not isinstance(data, dict):
                raise RuntimeError("offline")
            return data
        return hub_client.fetch_json(url, path)

    try:
        merged = assemble_hub(nodes, None, fetch_json)
    except Exception as exc:
        raise HTTPException(502, "汇总失败: %s" % exc)
    cards = list(merged.get("cards") or [])
    current = [str(c.get("id")) for c in cards if c.get("id")]
    order = hub_client.apply_order(hub_client.read_order(ORDER_PATH), current)
    merged["cards"] = sort_cards_by_order(cards, order)
    merged["card_order"] = order
    merged["time"] = ""
    return merged


class _OverviewFlight:
    def __init__(self) -> None:
        self.ev = threading.Event()
        self.payload: Optional[Dict[str, Any]] = None
        self.ready = False


_overview_lock = threading.Lock()
_overview_wait: Dict[str, _OverviewFlight] = {}


def _pull_channel_rows(node: Dict[str, str]):
    overview = _call(node, "/api/overview", timeout=8.0) or {}
    detail = _call(node, "/api/channels", timeout=8.0) or {}
    return node, overview, detail


def _build_overview(scope: str) -> Dict[str, Any]:
    nodes = _nodes()
    listed = _targets(scope)
    chosen_id = listed[0]["id"] if scope != "all" else ""
    with ThreadPoolExecutor(max_workers=max(1, len(nodes))) as pool:
        rows = list(pool.map(_pull_channel_rows, nodes))
    base: Dict[str, Any] = {}
    settings_name = ""
    for node, overview, _detail in rows:
        if scope != "all" and node["id"] != chosen_id:
            continue
        if isinstance(overview, dict) and overview.get("ai"):
            base = overview
            settings_name = node["name"]
            break
    channels: List[Dict[str, Any]] = []
    for node, overview, detail in rows:
        if scope != "all" and node["id"] != chosen_id:
            continue
        by_id = {str(c.get("id")): c for c in (detail.get("channels") or []) if c.get("id")}
        for row in overview.get("channels") or []:
            cid = str(row.get("id") or "")
            if not cid or "/" in cid:
                continue
            item = dict(row)
            extra = by_id.get(cid) or {}
            for key in ("url", "program", "iface", "category", "name", "enabled"):
                if extra.get(key) is not None:
                    item[key] = extra.get(key)
            item["node_id"] = node["id"]
            item["node_name"] = node["name"]
            item["channel_id"] = cid
            item["id"] = "%s/%s" % (node["id"], cid)
            channels.append(item)
    base = dict(base)
    base["channels"] = channels
    base["channel_total"] = len(channels)
    base["channel_enabled"] = sum(1 for c in channels if c.get("enabled", True))
    base["desktop"] = True
    base["settings_from"] = settings_name
    return base


@app.get("/api/overview")
def api_overview(scope: str = Query("all")) -> Dict[str, Any]:
    """页面会同时要 overview 和 channels。同一次刷新合并成一轮。"""
    with _overview_lock:
        waiting = _overview_wait.get(scope)
        if waiting is None:
            flight = _OverviewFlight()
            _overview_wait[scope] = flight
            owner = True
        else:
            flight = waiting
            owner = False
    if not owner:
        flight.ev.wait(25)
        if flight.ready and flight.payload is not None:
            return flight.payload
    try:
        payload = _build_overview(scope)
        flight.payload = payload
        flight.ready = True
        return payload
    finally:
        with _overview_lock:
            if _overview_wait.get(scope) is flight:
                _overview_wait.pop(scope, None)
        flight.ev.set()


@app.get("/api/channels")
def api_channels(scope: str = Query("all")) -> Dict[str, Any]:
    overview = api_overview(scope)
    return {"channels": overview.get("channels") or [], "desktop": True}


@app.get("/api/system/nics")
def api_nics(scope: str = Query("all")) -> Dict[str, Any]:
    node = _settings_node(scope)
    if not node:
        return {"nics": []}
    data = _call(node, "/api/system/nics", timeout=4.0) or {}
    return data if isinstance(data, dict) else {"nics": []}


@app.get("/api/system/perf")
def api_system_perf() -> Dict[str, Any]:
    node = _nodes()[0]
    data = _call(node, "/api/system/perf", timeout=3.0)
    if not data:
        raise HTTPException(502, "读不到性能")
    return data


@app.get("/api/hub/perf")
def api_hub_perf() -> Dict[str, Any]:
    nodes = _nodes()

    def pull(node: Dict[str, str]):
        return node["id"], _call(node, "/api/system/perf", timeout=3.0)

    found = {}
    with ThreadPoolExecutor(max_workers=max(1, len(nodes))) as pool:
        for nid, data in pool.map(pull, nodes):
            found[nid] = data

    def fetch_json(url: str, path: str):
        for node in nodes:
            if node["url"] == url and path == "/api/system/perf":
                data = found.get(node["id"])
                if not isinstance(data, dict):
                    raise RuntimeError("offline")
                return data
        return hub_client.fetch_json(url, path, timeout=3.0)

    return assemble_perf(nodes, None, fetch_json)


@app.get("/api/audio-levels")
def api_audio_levels() -> Dict[str, Any]:
    levels: Dict[str, Any] = {}
    for node in _nodes():
        data = _call(node, "/api/audio-levels", timeout=3.0) or {}
        for cid, row in (data.get("levels") or {}).items():
            levels["%s/%s" % (node["id"], cid)] = row
    return {"levels": levels}


@app.get("/api/snapshots")
def api_snapshots(
    limit: int = Query(40, ge=1, le=500),
    offset: int = Query(0, ge=0),
    channel_id: Optional[str] = None,
    scope: str = Query("all"),
) -> Dict[str, Any]:
    need = min(500, offset + limit)
    items: List[Dict[str, Any]] = []
    total = 0
    raw_channel = channel_id or ""
    if raw_channel and "/" in raw_channel:
        try:
            _nid, raw_channel = hub_client.split_uid(raw_channel)
        except ValueError:
            raw_channel = channel_id or ""
    for node in _targets(scope):
        qs = "limit=%d&offset=0&local=1" % need
        if raw_channel:
            qs += "&channel_id=" + quote(raw_channel, safe="")
        data = _call(node, "/api/snapshots?" + qs, timeout=4.0)
        if not data:
            continue
        try:
            total += int(data.get("total") or 0)
        except (TypeError, ValueError):
            total += len(data.get("snapshots") or [])
        for snap in data.get("snapshots") or []:
            fn = str(snap.get("filename") or "")
            cid = str(snap.get("channel_id") or "")
            if not is_alarm_snapshot(fn) or not cid:
                continue
            row = dict(snap)
            row["node_id"] = node["id"]
            row["node_name"] = node["name"]
            if node["name"] and node["name"] not in str(row.get("channel_name") or ""):
                row["channel_name"] = "%s %s" % (node["name"], row.get("channel_name") or cid)
            row["url"] = "/api/hub/snap/%s/%s/%s" % (node["id"], cid, fn)
            items.append(row)
    items.sort(key=lambda x: str(x.get("mtime_ts") or x.get("mtime") or ""), reverse=True)
    return {
        "snapshots": items[offset : offset + limit],
        "total": total or len(items),
        "offset": offset,
        "limit": limit,
    }


@app.get("/api/hub/snap/{node_id}/{channel_id}/{filename}")
def api_hub_snap(node_id: str, channel_id: str, filename: str) -> Response:
    if ".." in filename or "/" in filename or not filename.lower().endswith(".jpg"):
        raise HTTPException(404, "截图不存在")
    node = None
    for item in _nodes():
        if item["id"] == node_id:
            node = item
            break
    if node is None:
        raise HTTPException(404, "节点不存在")
    remote = "%s/api/snapshots/%s/%s" % (node["url"], quote(channel_id, safe=""), quote(filename, safe=""))
    try:
        blob = hub_client.fetch_bytes(remote)
    except Exception:
        raise HTTPException(502, "节点截图不可达")
    if not blob:
        raise HTTPException(404, "截图不存在")
    return Response(content=blob, media_type="image/jpeg")


@app.get("/api/alerts/history")
def api_alerts_history(request: Request, scope: str = Query("all")) -> Dict[str, Any]:
    try:
        limit = int(request.query_params.get("limit") or 100)
    except ValueError:
        limit = 100
    try:
        offset = int(request.query_params.get("offset") or 0)
    except ValueError:
        offset = 0
    limit = max(1, min(limit, 500))
    offset = max(0, offset)
    params = []
    for key, value in request.query_params.multi_items():
        if key in ("offset", "limit", "scope"):
            continue
        params.append((key, value))
    params.append(("offset", "0"))
    params.append(("limit", str(min(2000, offset + limit))))
    path = "/api/alerts/history?" + urlencode(params)
    alerts: List[Dict[str, Any]] = []
    total = 0
    source = "desktop"
    for node in _targets(scope):
        data = _call(node, path, timeout=8.0)
        if not data:
            continue
        source = str(data.get("source") or source)
        try:
            total += int(data.get("total") or 0)
        except (TypeError, ValueError):
            pass
        for ev in data.get("alerts") or []:
            if not isinstance(ev, dict):
                continue
            row = dict(ev)
            row["node_id"] = node["id"]
            row["node_name"] = node["name"]
            name = str(row.get("channel_name") or row.get("channel_id") or "")
            if node["name"] and not name.startswith(node["name"]):
                row["channel_name"] = "%s %s" % (node["name"], name)
            row["snapshot_url"] = hub_client.rewrite_snap_url(
                node["id"], str(row.get("snapshot_url") or "")
            )
            alerts.append(row)
    alerts.sort(key=lambda ev: str(ev.get("time") or ""), reverse=True)
    return {
        "source": source,
        "total": total or len(alerts),
        "offset": offset,
        "limit": limit,
        "alerts": alerts[offset : offset + limit],
    }


@app.get("/api/storage/detail")
def api_storage_detail(scope: str = Query("all")) -> Dict[str, Any]:
    nodes = _targets(scope) if scope != "all" else _nodes()
    total = {
        "logs_bytes": 0,
        "events_bytes": 0,
        "snapshots_bytes": 0,
        "data_bytes": 0,
        "total_bytes": 0,
        "sqlite": {"alerts_count": 0, "db_size_bytes": 0},
    }
    for node in nodes:
        data = _call(node, "/api/storage/detail", timeout=6.0) or {}
        for key in ("logs_bytes", "events_bytes", "snapshots_bytes", "data_bytes", "total_bytes"):
            try:
                total[key] += int(data.get(key) or 0)
            except (TypeError, ValueError):
                pass
        sql = data.get("sqlite") or {}
        try:
            total["sqlite"]["alerts_count"] += int(sql.get("alerts_count") or 0)
            total["sqlite"]["db_size_bytes"] += int(sql.get("db_size_bytes") or 0)
        except (TypeError, ValueError):
            pass
    names = "、".join(n["name"] for n in nodes)
    total["note"] = names
    return total


@app.post("/api/config/ai")
async def api_config_ai(request: Request) -> Dict[str, Any]:
    payload = await request.json()
    return _fanout(_scope_of(request), "/api/config/ai", payload)


@app.post("/api/config/defaults")
async def api_config_defaults(request: Request) -> Dict[str, Any]:
    payload = await request.json()
    return _fanout(_scope_of(request), "/api/config/defaults", payload)


@app.post("/api/config/channels")
async def api_create_channel(request: Request) -> Dict[str, Any]:
    node = _one(_scope_of(request))
    payload = await request.json()
    return _forward(node, "POST", "/api/config/channels", payload)


@app.post("/api/config/channels/{channel_key:path}")
async def api_update_channel(channel_key: str, request: Request) -> Dict[str, Any]:
    node_id, channel_id = _split_or_400(channel_key)
    node = _node_by_id(node_id)
    payload = await request.json()
    return _forward(node, "POST", "/api/config/channels/" + quote(channel_id, safe=""), payload)


@app.delete("/api/config/channels/{channel_key:path}")
def api_delete_channel(channel_key: str) -> Dict[str, Any]:
    node_id, channel_id = _split_or_400(channel_key)
    node = _node_by_id(node_id)
    return _forward(node, "DELETE", "/api/config/channels/" + quote(channel_id, safe=""))


@app.get("/api/config/export")
def api_export(fmt: str = Query("json"), scope: str = Query("all")) -> Response:
    node = _one(scope)
    path = "/api/config/export?fmt=" + quote(fmt, safe="")
    try:
        blob = hub_client.fetch_bytes(node["url"] + path, timeout=20.0)
    except Exception as exc:
        raise HTTPException(502, str(exc))
    media = "application/x-yaml" if fmt == "yaml" else "application/json"
    return Response(content=blob, media_type=media)


@app.post("/api/config/import")
async def api_import(request: Request) -> Dict[str, Any]:
    node = _one(_scope_of(request))
    payload = await request.json()
    return _forward(node, "POST", "/api/config/import", payload)


@app.post("/api/config/import/file")
async def api_import_file(request: Request) -> Dict[str, Any]:
    node = _one(_scope_of(request))
    mode = str(request.query_params.get("mode") or "merge")
    if mode not in ("merge", "replace"):
        raise HTTPException(400, "mode 必须是 merge 或 replace")
    body = await request.body()
    ctype = request.headers.get("content-type") or "application/octet-stream"
    try:
        return hub_client.post_bytes(
            node["url"],
            "/api/config/import/file?mode=" + quote(mode, safe=""),
            body,
            ctype,
            timeout=60,
        )
    except Exception as exc:
        raise HTTPException(502, "%s: %s" % (node.get("name") or node["id"], exc))


@app.post("/api/storage/clear")
async def api_storage_clear(request: Request) -> Dict[str, Any]:
    node = _one(_scope_of(request))
    payload = await request.json()
    return _forward(node, "POST", "/api/storage/clear", payload)


_now: Dict[str, Any] = {}


def _clip(value: Any, limit: int = 80) -> str:
    text = str(value or "").replace("\r", " ").replace("\n", " ").strip()
    return text[:limit]


def _play_view(playing: bool) -> Dict[str, Any]:
    if not playing:
        return {"playing": False}
    out = {"playing": True}
    out.update(_now)
    return out


@app.get("/api/desktop/play")
def api_desktop_play_status() -> Dict[str, Any]:
    return _play_view(player.reap_if_dead())


@app.post("/api/desktop/play")
async def api_desktop_play(request: Request) -> Dict[str, Any]:
    global _now
    body = await request.json()
    try:
        node_id, channel_id = hub_client.split_uid(str(body.get("id") or ""))
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    node = _node_by_id(node_id)
    name = _clip(body.get("name") or channel_id, 60)
    addr, port = player.parse_mcast(_clip(body.get("url"), 200))
    label = name
    if addr and port:
        label = "%s  %s:%s" % (name, addr, port)
    url = "%s/api/preview/%s" % (node["url"], quote(channel_id, safe=""))
    err = player.start_player(HERE, url, label)
    if err:
        _now = {}
        return {"ok": False, "playing": False, "fallback": "snap", "message": err}
    _now = {
        "id": "%s/%s" % (node_id, channel_id),
        "name": name,
        "node": _clip(body.get("node") or node.get("name") or node_id, 40),
        "addr": addr,
        "port": port,
        "program": _clip(body.get("program"), 20),
        "iface": _clip(body.get("iface"), 40),
        "bitrate": _clip(body.get("bitrate"), 40),
    }
    out = {"ok": True, "message": "正在本机播放 " + name}
    out.update(_play_view(True))
    return out


@app.post("/api/desktop/play/stop")
def api_desktop_stop() -> Dict[str, Any]:
    global _now
    player.stop_player()
    _now = {}
    return {"ok": True, "playing": False}


@app.post("/api/desktop/restart")
def api_desktop_restart(scope: str = Query("all")) -> Dict[str, Any]:
    return _fanout(scope, "/api/admin/restart", {})


@app.post("/api/desktop/model")
async def api_desktop_model(
    scope: str = Query("all"),
    restart: bool = Query(True),
    onnx: UploadFile = File(...),
    classes: UploadFile = File(...),
) -> Dict[str, Any]:
    onnx_bytes = await onnx.read()
    class_bytes = await classes.read()
    if len(onnx_bytes) < 1000 or len(onnx_bytes) > 200 * 1024 * 1024:
        raise HTTPException(400, "模型文件大小不对")
    try:
        parsed = json.loads(class_bytes.decode("utf-8"))
    except Exception:
        raise HTTPException(400, "类别文件不是 JSON")
    if not isinstance(parsed, dict) or not isinstance(parsed.get("classes"), list) or not parsed["classes"]:
        raise HTTPException(400, "类别文件里要有 classes 列表")
    lines = []
    failed = 0
    boundary_body = _multipart(onnx_bytes, class_bytes)
    for node in _targets(scope):
        try:
            _post_model(node["url"], boundary_body)
            lines.append("%s 模型已写入" % node["name"])
            if restart:
                hub_client.post_json(node["url"], "/api/admin/restart", {})
                lines.append("%s 正在重启监测" % node["name"])
        except Exception as exc:
            failed += 1
            lines.append("%s 失败: %s" % (node["name"], exc))
    if failed == len(_targets(scope)):
        raise HTTPException(502, "；".join(lines))
    return {"ok": failed == 0, "message": "；".join(lines)}


def _multipart(onnx_bytes: bytes, class_bytes: bytes) -> tuple:
    boundary = "----amdesktopmodel"
    chunks = []
    for name, filename, content, ctype in (
        ("onnx", "mosaic_detector.onnx", onnx_bytes, "application/octet-stream"),
        ("classes", "mosaic_detector.onnx.classes.json", class_bytes, "application/json"),
    ):
        chunks.append(("--" + boundary).encode())
        chunks.append(
            ('Content-Disposition: form-data; name="%s"; filename="%s"' % (name, filename)).encode()
        )
        chunks.append(("Content-Type: " + ctype).encode())
        chunks.append(b"")
        chunks.append(content)
    chunks.append(("--" + boundary + "--").encode())
    chunks.append(b"")
    body = b"\r\n".join(chunks)
    header = "multipart/form-data; boundary=" + boundary
    return body, header


def _post_model(base_url: str, packed: tuple) -> None:
    body, header = packed
    hub_client.post_bytes(base_url, "/api/admin/model", body, header, timeout=120)


def _node_by_id(node_id: str) -> Dict[str, str]:
    for node in _nodes():
        if node["id"] == node_id:
            return node
    raise HTTPException(404, "没有这台服务器")


def _split_or_400(channel_key: str) -> tuple:
    try:
        return hub_client.split_uid(channel_key)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
