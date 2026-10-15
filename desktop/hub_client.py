# -*- coding: utf-8 -*-
"""桌面汇总用的纯逻辑：节点、范围、截图地址、排列。不访问网络。"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.request import ProxyHandler, Request, build_opener


def load_nodes_from_payload(raw: Any) -> List[Dict[str, str]]:
    rows = raw.get("nodes") if isinstance(raw, dict) else raw
    if not isinstance(rows, list):
        raise ValueError("nodes.json 里没有监测机")
    out: List[Dict[str, str]] = []
    seen = set()
    for item in rows:
        if not isinstance(item, dict):
            raise ValueError("节点必须是对象")
        nid = str(item.get("id") or "").strip()
        name = str(item.get("name") or nid).strip()
        url = str(item.get("url") or "").strip().rstrip("/")
        if not nid or not url:
            raise ValueError("每台都要有 id 和 http 地址")
        if nid in seen:
            raise ValueError("节点 ID 重复: %s" % nid)
        seen.add(nid)
        out.append({"id": nid, "name": name or nid, "url": url})
    return out


def load_nodes(path: Path) -> List[Dict[str, str]]:
    return load_nodes_from_payload(json.loads(path.read_text(encoding="utf-8")))


def save_nodes(path: Path, nodes: List[Dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"nodes": nodes}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def split_uid(uid: str) -> Tuple[str, str]:
    text = str(uid or "")
    if "/" not in text:
        raise ValueError("节目编号里没有所属服务器")
    node_id, channel_id = text.split("/", 1)
    node_id = node_id.strip()
    channel_id = channel_id.strip()
    if not node_id or not channel_id or "/" in channel_id or ".." in channel_id:
        raise ValueError("节目编号无效")
    return node_id, channel_id


def pick_nodes(nodes: List[Dict[str, str]], scope: str) -> List[Dict[str, str]]:
    """scope 为 all 时三台都要；否则只留这一台。"""
    want = str(scope or "all").strip() or "all"
    if want == "all":
        return list(nodes)
    for node in nodes:
        if node["id"] == want or node["name"] == want:
            return [node]
    raise ValueError("没有这台服务器: %s" % want)


def rewrite_snap_url(node_id: str, url: str) -> str:
    text = str(url or "")
    prefix = "/api/snapshots/"
    if text.startswith(prefix):
        return "/api/hub/snap/%s/%s" % (node_id, text[len(prefix) :])
    return text


def apply_order(saved: List[str], current: List[str]) -> List[str]:
    have = set(current)
    out: List[str] = []
    seen = set()
    for cid in saved or []:
        cid = str(cid or "")
        if cid and cid in have and cid not in seen:
            out.append(cid)
            seen.add(cid)
    for cid in current or []:
        if cid and cid not in seen:
            out.append(cid)
            seen.add(cid)
    return out


def read_order(path: Path) -> List[str]:
    if not path.is_file():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    ids = raw.get("ids") if isinstance(raw, dict) else raw
    if not isinstance(ids, list):
        return []
    return [str(x) for x in ids if x]


def write_order(path: Path, ids: List[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps({"ids": ids}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    tmp.replace(path)


def opener():
    """直连监测机，不走开发环境里的 http 代理。"""
    return build_opener(ProxyHandler({}))


def fetch_json(base_url: str, path: str, timeout: float = 6.0) -> Optional[Dict[str, Any]]:
    url = base_url.rstrip("/") + path
    req = Request(url, headers={"Accept": "application/json"})
    with opener().open(req, timeout=timeout) as resp:
        raw = resp.read(2_000_000)
    data = json.loads(raw.decode("utf-8"))
    if not isinstance(data, dict):
        return None
    return data


def fetch_bytes(url: str, timeout: float = 8.0) -> bytes:
    req = Request(url)
    with opener().open(req, timeout=timeout) as resp:
        return resp.read(8_000_000)


def _error_text(exc: BaseException) -> str:
    detail = getattr(exc, "read", None)
    text = ""
    if callable(detail):
        try:
            raw = detail()
            if isinstance(raw, bytes):
                text = raw.decode("utf-8", "replace")
            else:
                text = str(raw or "")
        except Exception:
            text = ""
    text = (text or "").strip()
    if text.startswith("{") and "detail" in text:
        try:
            parsed = json.loads(text)
            if isinstance(parsed, dict) and parsed.get("detail"):
                return str(parsed["detail"])
        except ValueError:
            pass
    return text or str(exc)


def _read_json(raw: bytes) -> Dict[str, Any]:
    if not raw:
        return {"ok": True}
    data = json.loads(raw.decode("utf-8"))
    return data if isinstance(data, dict) else {"ok": True}


def post_json(base_url: str, path: str, payload: Any, timeout: float = 20.0) -> Dict[str, Any]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = Request(
        base_url.rstrip("/") + path,
        data=body,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    try:
        with opener().open(req, timeout=timeout) as resp:
            raw = resp.read(2_000_000)
    except Exception as exc:
        raise RuntimeError(_error_text(exc))
    return _read_json(raw)


def post_bytes(base_url: str, path: str, body: bytes, content_type: str, timeout: float = 120.0) -> Dict[str, Any]:
    req = Request(
        base_url.rstrip("/") + path,
        data=body,
        headers={"Content-Type": content_type, "Accept": "application/json"},
        method="POST",
    )
    try:
        with opener().open(req, timeout=timeout) as resp:
            raw = resp.read(2_000_000)
    except Exception as exc:
        raise RuntimeError(_error_text(exc))
    return _read_json(raw)


def post_delete(base_url: str, path: str, timeout: float = 20.0) -> Dict[str, Any]:
    req = Request(
        base_url.rstrip("/") + path,
        headers={"Accept": "application/json"},
        method="DELETE",
    )
    try:
        with opener().open(req, timeout=timeout) as resp:
            raw = resp.read(2_000_000)
    except Exception as exc:
        raise RuntimeError(_error_text(exc))
    return _read_json(raw)
