# -*- coding: utf-8 -*-
"""多机汇总。各监测机仍用自己的 channels.yaml；本模块只合并大屏数据。"""
from __future__ import annotations

import json
import re
from typing import Any, Callable, Dict, List, Optional
from urllib.request import Request, urlopen

_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")


def normalize_node_list(raw_nodes: Any) -> List[Dict[str, str]]:
    if not isinstance(raw_nodes, list):
        raise ValueError("nodes 必须是列表")
    out: List[Dict[str, str]] = []
    seen = set()
    empty_url = 0
    for item in raw_nodes:
        if not isinstance(item, dict):
            raise ValueError("节点必须是对象")
        nid = str(item.get("id") or "").strip()
        name = str(item.get("name") or nid).strip()
        url = str(item.get("url") or "").strip().rstrip("/")
        if not _ID_RE.match(nid):
            raise ValueError("节点 ID 只能用字母、数字、下划线和短横线")
        if nid in seen:
            raise ValueError("节点 ID 重复: %s" % nid)
        seen.add(nid)
        if not name:
            raise ValueError("节点名称不能为空")
        if len(name) > 32:
            name = name[:32]
        if url:
            if not (url.startswith("http://") or url.startswith("https://")):
                raise ValueError("节点地址须以 http:// 或 https:// 开头")
            if len(url) > 200:
                raise ValueError("节点地址过长")
        else:
            empty_url += 1
        out.append({"id": nid, "name": name, "url": url})
    if empty_url > 1:
        raise ValueError("地址留空表示本机，只能有一条")
    return out


def hub_is_active(nodes: List[Dict[str, str]]) -> bool:
    """至少有一台远程监测机时才合并。只有本机时与现在的单机页面相同。"""
    return any(n.get("url") for n in nodes)


def load_nodes_doc(text: str) -> List[Dict[str, str]]:
    import yaml

    doc = yaml.safe_load(text) or {}
    if not isinstance(doc, dict):
        raise ValueError("nodes.yaml 格式不对")
    return normalize_node_list(doc.get("nodes") or [])


def card_uid(node_id: str, channel_id: str) -> str:
    return "%s/%s" % (node_id, channel_id)


def _rewrite_remote_thumb(node_id: str, thumb: str) -> str:
    if not thumb or not thumb.startswith("/api/snapshots/"):
        return thumb
    path_q = thumb[len("/api/snapshots/") :]
    return "/api/hub/snap/%s/%s" % (node_id, path_q)


def _merge_stats(parts: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    by_type: Dict[str, int] = {}
    by_ch: Dict[str, Dict[str, Any]] = {}
    total = 0
    any_stats = False
    for part in parts:
        st = part.get("stats")
        if not st:
            continue
        any_stats = True
        total += int(st.get("total") or 0)
        node_name = part.get("node_name") or ""
        for row in st.get("by_type") or []:
            typ = str(row.get("type") or "")
            if not typ:
                continue
            by_type[typ] = by_type.get(typ, 0) + int(row.get("count") or 0)
        for row in st.get("by_channel") or []:
            cid = str(row.get("channel_id") or "")
            key = "%s/%s" % (part.get("node_id") or "", cid)
            name = row.get("channel_name") or cid
            if node_name:
                name = "%s · %s" % (node_name, name)
            cur = by_ch.get(key)
            count = int(row.get("count") or 0)
            if cur:
                cur["count"] += count
            else:
                by_ch[key] = {
                    "channel_id": key,
                    "channel_name": name,
                    "count": count,
                }
    if not any_stats:
        return None
    types = [{"type": k, "count": v} for k, v in by_type.items()]
    types.sort(key=lambda x: x["count"], reverse=True)
    chans = list(by_ch.values())
    chans.sort(key=lambda x: x["count"], reverse=True)
    return {"total": total, "by_type": types, "by_channel": chans[:20]}


def assemble_hub(
    nodes: List[Dict[str, str]],
    local_dash: Optional[Dict[str, Any]],
    fetch_json: Callable[[str, str], Optional[Dict[str, Any]]],
) -> Dict[str, Any]:
    """把各节点的 /api/dashboard 拼成一张大屏。远程失败只记节点离线，不丢其他节点。"""
    cards: List[Dict[str, Any]] = []
    node_rows: List[Dict[str, Any]] = []
    events: List[Dict[str, Any]] = []
    stat_parts: List[Dict[str, Any]] = []
    for node in nodes:
        nid = node["id"]
        name = node["name"]
        url = node.get("url") or ""
        dash = None
        ok = False
        if not url:
            dash = local_dash
            ok = dash is not None
        else:
            try:
                dash = fetch_json(url, "/api/dashboard")
            except Exception:
                dash = None
            ok = isinstance(dash, dict)
        n_cards = len((dash or {}).get("cards") or []) if ok else 0
        node_rows.append(
            {
                "id": nid,
                "name": name,
                "url": url,
                "ok": ok,
                "local": not url,
                "cards": n_cards,
            }
        )
        if not ok or not dash:
            continue
        stat_parts.append(
            {
                "node_id": nid,
                "node_name": name,
                "stats": dash.get("stats_24h"),
            }
        )
        for card in dash.get("cards") or []:
            origin = str(card.get("id") or "")
            copied = dict(card)
            copied["channel_id"] = origin
            copied["node_id"] = nid
            copied["node_name"] = name
            copied["id"] = card_uid(nid, origin)
            thumb = copied.get("thumb_url") or ""
            if url:
                copied["thumb_url"] = _rewrite_remote_thumb(nid, thumb)
                copied["preview_base"] = "/api/hub/snap/%s/%s" % (nid, origin)
            else:
                copied["preview_base"] = "/api/snapshots/%s" % origin
            cards.append(copied)
        for ev in (dash.get("recent_events") or [])[:40]:
            if not isinstance(ev, dict):
                continue
            row = dict(ev)
            row["node_id"] = nid
            row["node_name"] = name
            events.append(row)
    summary = {
        "total": len(cards),
        "green": sum(1 for c in cards if c.get("lamp") == "green"),
        "red": sum(1 for c in cards if c.get("lamp") == "red"),
        "yellow": sum(1 for c in cards if c.get("lamp") == "yellow"),
        "gray": sum(1 for c in cards if c.get("lamp") == "gray"),
    }
    return {
        "cards": cards,
        "summary": summary,
        "nodes": node_rows,
        "recent_events": events[:40],
        "stats_24h": _merge_stats(stat_parts),
        "hub": {"active": True},
    }


def fetch_json(base_url: str, path: str, timeout: float = 2.5) -> Optional[Dict[str, Any]]:
    url = base_url.rstrip("/") + path
    req = Request(url, headers={"Accept": "application/json"})
    with urlopen(req, timeout=timeout) as resp:
        raw = resp.read(2_000_000)
    data = json.loads(raw.decode("utf-8"))
    if not isinstance(data, dict):
        return None
    return data


def fetch_bytes(url: str, timeout: float = 4.0) -> Optional[bytes]:
    req = Request(url)
    with urlopen(req, timeout=timeout) as resp:
        return resp.read(6_000_000)
