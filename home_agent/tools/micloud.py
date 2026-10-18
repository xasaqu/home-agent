"""用米家 App 扫码登录小米账号，读取账号下米家设备的 token。

流程参照开源项目 Xiaomi Cloud Tokens Extractor（PiotrMachowski，MIT）：
1. account.xiaomi.com/longPolling/loginUrl 拿到登录二维码和长轮询地址；
2. 用户用米家 App 扫码确认后，长轮询返回 ssecurity 和跳转地址，跳转后拿到 serviceToken；
3. 用 RC4 加密签名请求 api.io.mi.com/app/home/device_list，返回每台设备的 token。

这是小米没有公开文档的接口，小米改了接口就可能失效。登录凭证只放在内存里，用完即丢，
只把各设备的 token 保存到本机的设备列表里。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import random
import string
import time
from typing import Any
from urllib.parse import urlparse

import httpx

log = logging.getLogger("home_agent.micloud")

LOGIN_URL = "https://account.xiaomi.com/longPolling/loginUrl"
SERVERS = {"cn": "https://api.io.mi.com/app", "sg": "https://sg.api.io.mi.com/app",
           "de": "https://de.api.io.mi.com/app", "us": "https://us.api.io.mi.com/app"}
AGENT = ("Android-7.1.1-1.0.0-ONEPLUS A3010-136-"
         + "".join(random.choices(string.ascii_uppercase, k=13)) + " APP/xiaomi.smarthome APPV/62830")


def _strip(text: str) -> dict[str, Any]:
    return json.loads(text.replace("&&&START&&&", "", 1))


# ---------------------------------------------------------------------------
# RC4 签名加密（米家 App 的请求格式）
# ---------------------------------------------------------------------------

def _rc4(key: bytes, data: bytes) -> bytes:
    s = list(range(256))
    j = 0
    for i in range(256):
        j = (j + s[i] + key[i % len(key)]) % 256
        s[i], s[j] = s[j], s[i]
    i = j = 0
    out = bytearray()
    # 米家协议要求先丢弃前 1024 字节的密钥流
    for n in range(1024 + len(data)):
        i = (i + 1) % 256
        j = (j + s[i]) % 256
        s[i], s[j] = s[j], s[i]
        if n >= 1024:
            out.append(data[n - 1024] ^ s[(s[i] + s[j]) % 256])
    return bytes(out)


def _nonce() -> str:
    return base64.b64encode(os.urandom(8) + int(time.time() / 60).to_bytes(4, "big")).decode()


def _signed_nonce(ssecurity: str, nonce: str) -> str:
    return base64.b64encode(hashlib.sha256(base64.b64decode(ssecurity) + base64.b64decode(nonce)).digest()).decode()


def _signature(url: str, method: str, signed_nonce: str, params: dict[str, str]) -> str:
    parts = [method.upper(), url.split("com", 1)[1].replace("/app/", "/")]
    parts += [f"{k}={v}" for k, v in params.items()]
    parts.append(signed_nonce)
    return base64.b64encode(hashlib.sha1("&".join(parts).encode()).digest()).decode()


def _encrypt_params(url: str, ssecurity: str, params: dict[str, str]) -> dict[str, str]:
    nonce = _nonce()
    sn = _signed_nonce(ssecurity, nonce)
    params = dict(params)
    params["rc4_hash__"] = _signature(url, "POST", sn, params)
    key = base64.b64decode(sn)
    params = {k: base64.b64encode(_rc4(key, v.encode())).decode() for k, v in params.items()}
    params.update(signature=_signature(url, "POST", sn, params), ssecurity=ssecurity, _nonce=nonce)
    params["_sn"] = sn  # 只在本地用来解密响应，发送前去掉
    return params


# ---------------------------------------------------------------------------
# 扫码登录会话
# ---------------------------------------------------------------------------

class MiCloudLogin:
    """一次扫码登录。status: idle / waiting / fetching / done / error / expired"""

    def __init__(self) -> None:
        self.status = "idle"
        self.message = ""
        self.login_url = ""
        self.devices: list[dict[str, Any]] = []
        self._task: asyncio.Task | None = None

    def info(self) -> dict[str, Any]:
        return {"status": self.status, "message": self.message, "login_url": self.login_url,
                "devices": [{k: d.get(k) for k in ("name", "model", "localip", "mac", "matched")}
                            for d in self.devices]}

    async def start(self, on_devices) -> dict[str, Any]:
        if self._task and not self._task.done():
            self._task.cancel()
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.get(LOGIN_URL, params={
                "_qrsize": "480", "qs": "%3Fsid%3Dxiaomiio%26_json%3Dtrue",
                "callback": "https://sts.api.io.mi.com/sts", "_hasLogo": "false", "sid": "xiaomiio",
                "serviceParam": "", "_locale": "zh_CN", "_dc": str(int(time.time() * 1000))})
        data = _strip(r.text)
        if data.get("code") != 0 or not data.get("lp"):
            raise RuntimeError(f"获取二维码失败：{data.get('desc') or data}")
        self.login_url, self.devices = data["loginUrl"], []
        self.status, self.message = "waiting", "请用手机上的米家 App 扫描二维码，并在手机上确认登录"
        self._task = asyncio.create_task(self._run(data["lp"], int(data.get("timeout", 300)), on_devices))
        return self.info()

    async def _run(self, lp_url: str, timeout: int, on_devices) -> None:
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(timeout + 10, connect=15)) as c:
                r = await c.get(lp_url)
                if r.status_code != 200:
                    self.status, self.message = "expired", "二维码已过期，请重新获取"
                    return
                auth = _strip(r.text)
                if not auth.get("ssecurity") or not auth.get("location"):
                    self.status, self.message = "error", f"登录没有成功：{auth.get('desc') or '未知原因'}"
                    return
                self.status, self.message = "fetching", "已登录，正在读取设备列表…"
                r = await c.get(auth["location"], headers={"Host": urlparse(auth["location"]).netloc})
                service_token = r.cookies.get("serviceToken") or c.cookies.get("serviceToken")
                if not service_token:
                    self.status, self.message = "error", "登录后没有拿到 serviceToken，请重试"
                    return
                devices: list[dict[str, Any]] = []
                for region, base in SERVERS.items():  # 国内账号在 cn，其它地区依次尝试
                    found = await self._device_list(c, base, str(auth["userId"]), auth["ssecurity"], service_token)
                    for d in found:
                        d["region"] = region
                    devices += found
                    if devices:
                        break
            self.devices = [d for d in devices if d.get("token")]
            matched = on_devices(self.devices)
            for d in self.devices:
                d["matched"] = d.get("did") in matched
            self.status = "done"
            self.message = (f"读取到 {len(self.devices)} 台米家设备的 token，已自动填到设备列表里。"
                            if self.devices else "这个小米账号下没有找到可以用 token 控制的设备。")
        except asyncio.CancelledError:
            raise
        except httpx.TimeoutException:
            self.status, self.message = "expired", "等待扫码超时，请重新获取二维码"
        except Exception as e:
            log.exception("micloud login failed")
            self.status, self.message = "error", f"出错了：{e}"

    async def _device_list(self, c: httpx.AsyncClient, base: str, user_id: str, ssecurity: str,
                           service_token: str) -> list[dict[str, Any]]:
        url = base + "/home/device_list"
        params = _encrypt_params(url, ssecurity, {"data": json.dumps(
            {"getVirtualModel": True, "getHuamiDevices": 1, "get_split_device": False, "support_smart_home": True})})
        sn = params.pop("_sn")
        cookies = {"userId": user_id, "yetAnotherServiceToken": service_token, "serviceToken": service_token,
                   "locale": "zh_CN", "timezone": "GMT+08:00", "is_daylight": "0", "dst_offset": "0",
                   "channel": "MI_APP_STORE"}
        headers = {"Accept-Encoding": "identity", "User-Agent": AGENT,
                   "Content-Type": "application/x-www-form-urlencoded",
                   "x-xiaomi-protocal-flag-cli": "PROTOCAL-HTTP2", "MIOT-ENCRYPT-ALGORITHM": "ENCRYPT-RC4"}
        r = await c.post(url, data=params, headers=headers, cookies=cookies)
        if r.status_code != 200:
            return []
        try:
            body = json.loads(_rc4(base64.b64decode(sn), base64.b64decode(r.text)))
        except Exception:
            return []
        return list((body.get("result") or {}).get("list") or [])
