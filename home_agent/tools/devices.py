"""设备注册表、权限和暴露给 Agent 的设备工具。

权限分三级：none（只知道它存在）→ view（可查看状态）→ control（可操作）。
Agent 只能“申请”权限，批准必须由用户在网页或微信里完成。
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
import uuid
from collections.abc import Callable
from typing import Any

from agents import function_tool

from .. import config
from .adapters import ADAPTERS, AdapterError
from .discovery import discover_async, local_network_info

LEVELS = {"none": 0, "view": 1, "control": 2}
_lock = threading.Lock()
# 新权限申请的通知回调（服务器启动时注册，例如推送到网页和微信）
request_listeners: list[Callable[[dict[str, Any]], None]] = []


# ---------------------------------------------------------------------------
# 持久化
# ---------------------------------------------------------------------------

def _load() -> dict[str, Any]:
    if config.DEVICES_FILE.exists():
        return json.loads(config.DEVICES_FILE.read_text(encoding="utf-8"))
    return {"devices": {}, "requests": {}, "log": []}


def _save(db: dict[str, Any]) -> None:
    tmp = config.DEVICES_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(db, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        tmp.chmod(0o600)  # 里面可能有设备凭证
    except OSError:
        pass
    tmp.replace(config.DEVICES_FILE)


def device_id_for(d: dict[str, Any]) -> str:
    key = d.get("mac") or d["ip"]
    return hashlib.sha1(key.encode()).hexdigest()[:8]


def public_view(d: dict[str, Any]) -> dict[str, Any]:
    """去掉凭证后的设备信息。"""
    return {k: v for k, v in d.items() if k != "credentials"} | {
        "has_credentials": bool(d.get("credentials"))}


def list_all() -> list[dict[str, Any]]:
    with _lock:
        return [public_view(d) for d in _load()["devices"].values()]


def get(device_id: str) -> dict[str, Any]:
    with _lock:
        d = _load()["devices"].get(device_id)
    if not d:
        raise KeyError(f"找不到设备 {device_id}")
    return d


def merge_discovered(found: list[dict[str, Any]]) -> list[dict[str, Any]]:
    now = time.time()
    with _lock:
        db = _load()
        for f in found:
            if not f.get("ip"):
                continue
            did = device_id_for(f)
            existing = db["devices"].get(did, {})
            if existing.get("adapter") == "unknown":  # 之前猜不出来的，用这次的猜测
                existing.pop("adapter")
            db["devices"][did] = {
                "permission": "none",
                "adapter": f.get("suggested_adapter", "unknown"),
                **existing,
                **{k: v for k, v in f.items() if k != "suggested_adapter"},
                "id": did,
                "name": existing.get("alias") or f.get("name") or f["ip"],
                "last_seen": now,
            }
        _save(db)
        return [public_view(d) for d in db["devices"].values()]


def apply_miio_tokens(cloud: list[dict[str, Any]]) -> set[str]:
    """把从小米账号读到的 token 填到对应设备上（按 MAC 或 IP 匹配，找不到就新加一台）。
    只保存 token，不会改变设备的权限，仍然需要用户点“授权”。返回匹配上的小米设备 did。"""
    from .discovery import normalize_mac

    matched: set[str] = set()
    with_ip = [c for c in cloud if c.get("localip") and c.get("token")]
    if not with_ip:
        return matched
    known = {(d.get("mac") or ""): d["id"] for d in list_all() if d.get("mac")}
    by_ip = {d["ip"]: d["id"] for d in list_all()}
    new = [{"ip": c["localip"], "mac": normalize_mac(c["mac"]) if c.get("mac") else None, "name": c.get("name"),
            "suggested_adapter": "miio"}
           for c in with_ip
           if not (c.get("mac") and normalize_mac(c["mac"]) in known) and c["localip"] not in by_ip]
    if new:
        merge_discovered([{k: v for k, v in n.items() if v} for n in new])
    with _lock:
        db = _load()
        for c in with_ip:
            mac = normalize_mac(c["mac"]) if c.get("mac") else ""
            dev = next((d for d in db["devices"].values() if mac and d.get("mac") == mac), None) \
                or next((d for d in db["devices"].values() if d.get("ip") == c["localip"]), None)
            if not dev:
                continue
            if dev.get("adapter") in (None, "unknown", "miio"):
                dev["adapter"] = "miio"
                dev["credentials"] = {**(dev.get("credentials") or {}), "token": c["token"],
                                      "model": c.get("model", "")}
                dev["model"] = c.get("model", dev.get("model"))
                if not dev.get("alias") and c.get("name"):
                    dev["name"] = c["name"]
                matched.add(str(c.get("did")))
        _save(db)
    _log({"event": "miio_tokens", "count": len(matched), "by": "ui"})
    return matched


def update(device_id: str, **fields: Any) -> dict[str, Any]:
    with _lock:
        db = _load()
        d = db["devices"][device_id]
        d.update(fields)
        _save(db)
        return public_view(d)


def remove(device_id: str) -> None:
    with _lock:
        db = _load()
        db["devices"].pop(device_id, None)
        _save(db)


def _log(entry: dict[str, Any]) -> None:
    with _lock:
        db = _load()
        db["log"] = (db["log"] + [{"ts": time.time(), **entry}])[-500:]
        _save(db)


def recent_log(n: int = 50) -> list[dict[str, Any]]:
    with _lock:
        return _load()["log"][-n:]


# ---------------------------------------------------------------------------
# 授权
# ---------------------------------------------------------------------------

def create_request(device_id: str, level: str, reason: str, source: str) -> dict[str, Any]:
    if level not in ("view", "control"):
        raise ValueError("level 只能是 view 或 control")
    get(device_id)
    for r in pending_requests():  # 同一设备同一级别只保留一个待批准申请
        if r["device_id"] == device_id and r["level"] == level:
            return r
    req = {"id": uuid.uuid4().hex[:6], "device_id": device_id, "level": level,
           "reason": reason, "source": source, "ts": time.time(), "status": "pending"}
    with _lock:
        db = _load()
        db["requests"][req["id"]] = req
        _save(db)
        req["device_name"] = db["devices"][device_id]["name"]
    for fn in request_listeners:
        try:
            fn(req)
        except Exception:
            pass
    return req


def pending_requests() -> list[dict[str, Any]]:
    with _lock:
        db = _load()
        return [r | {"device_name": db["devices"].get(r["device_id"], {}).get("name", "?")}
                for r in db["requests"].values() if r["status"] == "pending"]


async def grant(device_id: str, level: str, adapter: str | None = None,
                credentials: dict[str, str] | None = None, by: str = "ui") -> dict[str, Any]:
    """用户授权：可同时指定适配器和凭证，适配器的配对流程在这里执行。"""
    if level not in LEVELS:
        raise ValueError("level 只能是 none / view / control")
    d = get(device_id)
    adapter = adapter or d.get("adapter", "unknown")
    creds = d.get("credentials", {})
    if level != "none" and adapter in ADAPTERS:
        creds = await ADAPTERS[adapter].pair({**d, "credentials": creds}, credentials or creds)
    with _lock:
        db = _load()
        dev = db["devices"][device_id]
        dev.update(permission=level, adapter=adapter, credentials=creds)
        for r in db["requests"].values():
            if r["device_id"] == device_id and r["status"] == "pending" and LEVELS[r["level"]] <= LEVELS[level]:
                r["status"] = "approved"
        _save(db)
        result = public_view(dev)
    _log({"event": "grant", "device_id": device_id, "level": level, "by": by})
    return result


def deny_request(req_id: str) -> None:
    with _lock:
        db = _load()
        if req_id in db["requests"]:
            db["requests"][req_id]["status"] = "denied"
        _save(db)


async def approve_request(req_id: str, by: str) -> dict[str, Any]:
    with _lock:
        req = _load()["requests"].get(req_id)
    if not req or req["status"] != "pending":
        raise KeyError("没有这个待处理的申请")
    return await grant(req["device_id"], req["level"], by=by)


def _require(d: dict[str, Any], level: str) -> None:
    if LEVELS.get(d.get("permission", "none"), 0) < LEVELS[level]:
        raise PermissionError(
            f"设备「{d['name']}」当前权限为 {d.get('permission', 'none')}，需要 {level}。"
            f"请调用 request_device_access 申请，并等待用户批准。")
    if d.get("adapter") not in ADAPTERS:
        raise AdapterError(f"设备「{d['name']}」还没有可用的控制方式，需要用户在网页里为它选择适配器。")


async def read_state(device_id: str) -> dict[str, Any]:
    d = get(device_id)
    _require(d, "view")
    return await ADAPTERS[d["adapter"]].get_state(d)


async def do_control(device_id: str, action: str, params: dict[str, Any], by: str) -> dict[str, Any]:
    d = get(device_id)
    _require(d, "control")
    result = await ADAPTERS[d["adapter"]].control(d, action, params)
    if result.get("discovered"):  # 例如小米路由器返回的已连接设备
        merge_discovered(result.pop("discovered"))
    _log({"event": "control", "device_id": device_id, "action": action, "params": params, "by": by})
    return result


# ---------------------------------------------------------------------------
# 暴露给 Agent 的工具
# ---------------------------------------------------------------------------

def _json(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str)[:12000]


@function_tool
async def discover_devices(sweep: bool = False, timeout_seconds: float = 3.0) -> str:
    """主动扫描当前局域网里的设备（mDNS、SSDP/UPnP、Yeelight 广播、ARP 表）。

    Args:
        sweep: 为 True 时先向本网段每个地址发一个空包以填充 ARP 表，能发现更多不广播的设备，耗时多几秒。
        timeout_seconds: 每种发现方式等待响应的秒数，2-8 之间比较合适。
    """
    found = await discover_async(sweep, max(1.0, min(timeout_seconds, 10.0)))
    devices = merge_discovered(found)
    errors = [f for f in found if f.get("name") == "discovery_errors"]
    return _json({"network": local_network_info(), "devices": devices, "errors": errors})


@function_tool
def list_devices() -> str:
    """列出已经发现过的所有设备，包括它们的 id、IP、适配器和当前权限（none/view/control）。"""
    return _json({"devices": list_all(), "pending_requests": pending_requests(),
                  "adapters": {k: a.actions for k, a in ADAPTERS.items()}})


@function_tool
def request_device_access(device_id: str, level: str, reason: str) -> str:
    """为某台设备申请查看(view)或操作(control)权限。申请会推送给用户，由用户批准后才生效。

    Args:
        device_id: 设备 id（来自 list_devices）。
        level: "view" 或 "control"。
        reason: 向用户说明为什么需要这个权限。
    """
    req = create_request(device_id, level, reason, source="agent")
    return _json({"request": req, "note": "已提交申请，请提醒用户在网页的“设备”面板，或在微信里回复 "
                                          f"/approve {req['id']} 批准。"})


@function_tool
async def get_device_status(device_id: str) -> str:
    """读取设备的当前状态（需要 view 权限）。

    Args:
        device_id: 设备 id。
    """
    try:
        return _json(await read_state(device_id))
    except (PermissionError, AdapterError, KeyError) as e:
        return _json({"error": str(e)})
    except Exception as e:
        return _json({"error": f"设备通信失败: {e!r}"})


@function_tool
async def control_device(device_id: str, action: str, params_json: str = "{}") -> str:
    """操作设备（需要 control 权限）。可用动作见 list_devices 返回的 adapters。

    Args:
        device_id: 设备 id。
        action: 动作名，例如 on / off / brightness / call_service。
        params_json: 动作参数的 JSON 字符串，例如 {"value": 50}。
    """
    try:
        params = json.loads(params_json or "{}")
        return _json(await do_control(device_id, action, params, by="agent"))
    except (PermissionError, AdapterError, KeyError, ValueError) as e:
        return _json({"error": str(e)})
    except Exception as e:
        return _json({"error": f"设备通信失败: {e!r}"})


DEVICE_TOOLS = [discover_devices, list_devices, request_device_access, get_device_status, control_device]
