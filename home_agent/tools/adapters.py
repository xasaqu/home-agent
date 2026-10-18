"""设备适配器：每种设备都只走它自己公开文档里的本地 API。

适配器需要的凭证（Hue 用户名、Home Assistant 令牌、Shelly 密码、米家 token、路由器密码）都由用户在
网页里授权时提供或通过设备自身的配对流程获得，Agent 不会去猜或绕过。
访问局域网设备时不走系统代理（trust_env=False），否则会被代理软件转发出去。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx


class AdapterError(Exception):
    pass


class Adapter:
    name = "base"
    # 授权时需要用户填写的字段：{字段名: 说明}
    credential_fields: dict[str, str] = {}
    # 支持的动作及参数说明，会展示给大模型
    actions: dict[str, str] = {}

    async def pair(self, device: dict[str, Any], creds: dict[str, str]) -> dict[str, str]:
        """授权时调用，返回要保存的凭证。默认直接保存用户填写的内容。"""
        return creds

    async def get_state(self, device: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    async def control(self, device: dict[str, Any], action: str, params: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Yeelight：官方 LAN 协议，TCP 55443，每行一个 JSON
# ---------------------------------------------------------------------------

class YeelightAdapter(Adapter):
    name = "yeelight"
    actions = {
        "on": "开灯",
        "off": "关灯",
        "toggle": "切换开关",
        "brightness": "调亮度，params: {\"value\": 1-100}",
        "color_temp": "色温，params: {\"value\": 1700-6500}",
        "rgb": "颜色，params: {\"value\": \"#RRGGBB\"}",
    }

    async def _call(self, device: dict[str, Any], method: str, params: list[Any]) -> Any:
        port = device.get("port") or 55443
        reader, writer = await asyncio.wait_for(asyncio.open_connection(device["ip"], port), 3)
        try:
            writer.write((json.dumps({"id": 1, "method": method, "params": params}) + "\r\n").encode())
            await writer.drain()
            while True:
                line = await asyncio.wait_for(reader.readline(), 3)
                msg = json.loads(line)
                if msg.get("id") == 1:  # 跳过灯主动推送的 props 通知
                    break
        finally:
            writer.close()
        if "error" in msg:
            raise AdapterError(str(msg["error"]))
        return msg.get("result")

    async def get_state(self, device):
        keys = ["power", "bright", "ct", "rgb", "color_mode", "name"]
        values = await self._call(device, "get_prop", keys)
        return dict(zip(keys, values))

    async def control(self, device, action, params):
        v = params.get("value")
        if action in ("on", "off"):
            r = await self._call(device, "set_power", [action, "smooth", 300])
        elif action == "toggle":
            r = await self._call(device, "toggle", [])
        elif action == "brightness":
            r = await self._call(device, "set_bright", [max(1, min(100, int(v))), "smooth", 300])
        elif action == "color_temp":
            r = await self._call(device, "set_ct_abx", [max(1700, min(6500, int(v))), "smooth", 300])
        elif action == "rgb":
            r = await self._call(device, "set_rgb", [int(str(v).lstrip("#"), 16), "smooth", 300])
        else:
            raise AdapterError(f"不支持的动作: {action}")
        return {"result": r}


# ---------------------------------------------------------------------------
# Philips Hue：本地 REST API，需要按桥上的物理按钮完成配对
# ---------------------------------------------------------------------------

class HueAdapter(Adapter):
    name = "hue"
    actions = {
        "on": "开灯，params: {\"light\": 灯编号}",
        "off": "关灯，params: {\"light\": 灯编号}",
        "brightness": "亮度，params: {\"light\": 灯编号, \"value\": 1-254}",
    }

    async def pair(self, device, creds):
        if creds.get("username"):
            return creds
        async with httpx.AsyncClient(trust_env=False, timeout=5) as c:
            r = await c.post(f"http://{device['ip']}/api", json={"devicetype": "home_agent#local"})
        data = r.json()[0]
        if "error" in data:
            raise AdapterError("请先按一下 Hue 桥上的圆形按钮，然后在 30 秒内再点授权。")
        return {"username": data["success"]["username"]}

    def _base(self, device):
        user = device.get("credentials", {}).get("username")
        if not user:
            raise AdapterError("Hue 桥还没有配对")
        return f"http://{device['ip']}/api/{user}"

    async def get_state(self, device):
        async with httpx.AsyncClient(trust_env=False, timeout=5) as c:
            lights = (await c.get(f"{self._base(device)}/lights")).json()
        return {k: {"name": v["name"], **{x: v["state"].get(x) for x in ("on", "bri", "reachable")}}
                for k, v in lights.items()}

    async def control(self, device, action, params):
        light = params.get("light")
        if light is None:
            raise AdapterError("需要指定 light 编号，可先查询状态")
        body: dict[str, Any] = {}
        if action in ("on", "off"):
            body["on"] = action == "on"
        elif action == "brightness":
            body = {"on": True, "bri": max(1, min(254, int(params["value"])))}
        else:
            raise AdapterError(f"不支持的动作: {action}")
        async with httpx.AsyncClient(trust_env=False, timeout=5) as c:
            r = await c.put(f"{self._base(device)}/lights/{light}/state", json=body)
        return {"result": r.json()}


# ---------------------------------------------------------------------------
# Shelly：本地 HTTP API（Gen2+ RPC，回退 Gen1）。设备开了认证时用 Digest。
# ---------------------------------------------------------------------------

class ShellyAdapter(Adapter):
    name = "shelly"
    credential_fields = {"password": "设备开启了认证时填写，未开启可留空"}
    actions = {
        "on": "打开继电器，params: {\"channel\": 0}",
        "off": "关闭继电器，params: {\"channel\": 0}",
        "toggle": "切换，params: {\"channel\": 0}",
    }

    def _auth(self, device):
        pw = device.get("credentials", {}).get("password")
        return httpx.DigestAuth("admin", pw) if pw else None

    async def get_state(self, device):
        async with httpx.AsyncClient(trust_env=False, timeout=5, auth=self._auth(device)) as c:
            r = await c.get(f"http://{device['ip']}/rpc/Shelly.GetStatus")
            if r.status_code == 404:
                r = await c.get(f"http://{device['ip']}/status")
        r.raise_for_status()
        return r.json()

    async def control(self, device, action, params):
        ch = int(params.get("channel", 0))
        async with httpx.AsyncClient(trust_env=False, timeout=5, auth=self._auth(device)) as c:
            if action == "toggle":
                r = await c.get(f"http://{device['ip']}/rpc/Switch.Toggle", params={"id": ch})
            elif action in ("on", "off"):
                r = await c.get(f"http://{device['ip']}/rpc/Switch.Set",
                                params={"id": ch, "on": str(action == "on").lower()})
            else:
                raise AdapterError(f"不支持的动作: {action}")
            if r.status_code == 404:  # Gen1
                r = await c.get(f"http://{device['ip']}/relay/{ch}", params={"turn": action})
        r.raise_for_status()
        return {"result": r.json()}


# ---------------------------------------------------------------------------
# Home Assistant：如果家里已有 HA，可以通过它控制所有已接入的设备
# ---------------------------------------------------------------------------

class HomeAssistantAdapter(Adapter):
    name = "homeassistant"
    credential_fields = {
        "token": "HA 用户资料页底部生成的长期访问令牌",
        "base_url": "可选，默认 http://<IP>:8123",
        "entities": "允许管家操作的设备（实体 id，逗号分隔，例如 light.living_room,switch.fan）。"
                    "不填则只能查看、不能操作，避免一次授权就能控制门锁等所有设备",
    }
    actions = {
        "call_service": "调用服务，params: {\"domain\": \"light\", \"service\": \"turn_on\", "
                        "\"entity_id\": \"light.living_room\", \"data\": {...可选}}。"
                        "只能操作用户授权时列出的实体",
    }

    @staticmethod
    def _allowed(device) -> set[str]:
        raw = device.get("credentials", {}).get("entities") or ""
        return {e.strip() for e in raw.replace("，", ",").split(",") if e.strip()}

    def _client(self, device):
        creds = device.get("credentials", {})
        if not creds.get("token"):
            raise AdapterError("缺少 Home Assistant 令牌")
        base = creds.get("base_url") or f"http://{device['ip']}:8123"
        return httpx.AsyncClient(trust_env=False, base_url=base, timeout=10,
                                 headers={"Authorization": f"Bearer {creds['token']}"})

    async def pair(self, device, creds):
        device = {**device, "credentials": creds}
        async with self._client(device) as c:
            r = await c.get("/api/")
        if r.status_code != 200:
            raise AdapterError(f"令牌校验失败: HTTP {r.status_code}")
        return creds

    async def get_state(self, device):
        async with self._client(device) as c:
            states = (await c.get("/api/states")).json()
        return {"entities": [
            {"entity_id": s["entity_id"], "state": s["state"],
             "name": s.get("attributes", {}).get("friendly_name", "")}
            for s in states
            if s["entity_id"].split(".")[0] in
            ("light", "switch", "climate", "cover", "fan", "lock", "media_player", "sensor", "binary_sensor", "vacuum")
        ]}

    async def control(self, device, action, params):
        if action != "call_service":
            raise AdapterError(f"不支持的动作: {action}")
        allowed = self._allowed(device)
        if not allowed:
            raise AdapterError("这个 Home Assistant 还没有指定允许操作的设备，请让用户在授权里填写 entities")
        targets = params.get("entity_id")
        targets = [targets] if isinstance(targets, str) else list(targets or [])
        if not targets:
            raise AdapterError("必须指定 entity_id")
        denied = [t for t in targets if t not in allowed]
        if denied:
            raise AdapterError(f"没有操作这些设备的授权: {', '.join(denied)}")
        body = dict(params.get("data") or {})
        # 不允许通过 data 里的 target/area/device 绕开上面的设备限制
        for k in ("entity_id", "area_id", "device_id", "floor_id", "label_id", "target"):
            body.pop(k, None)
        body["entity_id"] = targets
        async with self._client(device) as c:
            r = await c.post(f"/api/services/{params['domain']}/{params['service']}", json=body)
        r.raise_for_status()
        return {"result": r.json()}


# ---------------------------------------------------------------------------
# 通用 HTTP：给自己有 HTTP 接口的设备（ESPHome、自制设备等）手动配置
# ---------------------------------------------------------------------------

class GenericHttpAdapter(Adapter):
    name = "http"
    credential_fields = {
        "config": "JSON：{\"status_url\": \"http://...\", \"headers\": {...}, "
                  "\"actions\": {\"on\": {\"method\": \"POST\", \"url\": \"http://...\", \"json\": {...}}}}",
    }
    actions = {"<自定义>": "动作名来自用户在授权时填写的 config.actions"}

    def _cfg(self, device):
        raw = device.get("credentials", {}).get("config") or "{}"
        return json.loads(raw) if isinstance(raw, str) else raw

    async def pair(self, device, creds):
        json.loads(creds.get("config") or "{}")  # 校验 JSON
        return creds

    async def get_state(self, device):
        cfg = self._cfg(device)
        if not cfg.get("status_url"):
            raise AdapterError("未配置 status_url")
        async with httpx.AsyncClient(trust_env=False, timeout=5, headers=cfg.get("headers")) as c:
            r = await c.get(cfg["status_url"])
        try:
            return r.json()
        except ValueError:
            return {"status_code": r.status_code, "text": r.text[:2000]}

    async def control(self, device, action, params):
        cfg = self._cfg(device)
        spec = (cfg.get("actions") or {}).get(action)
        if not spec:
            raise AdapterError(f"未配置的动作: {action}，可用: {list((cfg.get('actions') or {}).keys())}")
        async with httpx.AsyncClient(trust_env=False, timeout=5, headers=cfg.get("headers")) as c:
            r = await c.request(spec.get("method", "POST"), spec["url"], json=spec.get("json"))
        return {"status_code": r.status_code, "text": r.text[:2000]}


ADAPTERS: dict[str, Adapter] = {a.name: a for a in (
    YeelightAdapter(), HueAdapter(), ShellyAdapter(), HomeAssistantAdapter(), GenericHttpAdapter(),
)}
