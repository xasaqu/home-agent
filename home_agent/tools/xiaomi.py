"""小米设备：米家 miio 局域网协议，以及小米路由器的本地管理接口。

两者都需要用户自己提供凭证：
- miio：设备 token（32 位十六进制），由用户从自己的小米账号里取出；
- 路由器：管理后台密码，和网页登录 http://192.168.31.1 用的是同一个。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import random
import re
import socket
import struct
import time
from typing import Any

import httpx
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from .adapters import Adapter, AdapterError
from .discovery import normalize_mac

MIIO_PORT = 54321
HELLO = bytes.fromhex("21310020" + "ff" * 28)


# ---------------------------------------------------------------------------
# miio 协议
# ---------------------------------------------------------------------------

def _md5(b: bytes) -> bytes:
    return hashlib.md5(b).digest()


class MiioClient:
    """miio 二进制协议：32 字节包头 + AES-128-CBC 加密的 JSON。"""

    def __init__(self, ip: str, token: str, timeout: float = 3.0):
        try:
            self.token = bytes.fromhex(token.strip())
        except ValueError:
            raise AdapterError("token 必须是 32 位十六进制字符串")
        if len(self.token) != 16:
            raise AdapterError("token 必须是 32 位十六进制字符串")
        self.ip, self.timeout = ip, timeout
        self.key = _md5(self.token)
        self.iv = _md5(self.key + self.token)
        self._id = random.randint(1, 9000)

    def _cipher(self) -> Cipher:
        return Cipher(algorithms.AES(self.key), modes.CBC(self.iv))

    def _encrypt(self, data: bytes) -> bytes:
        p = padding.PKCS7(128).padder()
        enc = self._cipher().encryptor()
        return enc.update(p.update(data) + p.finalize()) + enc.finalize()

    def _decrypt(self, data: bytes) -> bytes:
        dec = self._cipher().decryptor()
        raw = dec.update(data) + dec.finalize()
        u = padding.PKCS7(128).unpadder()
        return u.update(raw) + u.finalize()

    def _send_recv(self, sock: socket.socket, packet: bytes) -> bytes:
        sock.sendto(packet, (self.ip, MIIO_PORT))
        data, _ = sock.recvfrom(4096)
        return data

    def call(self, method: str, params: Any = None) -> Any:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(self.timeout)
        try:
            try:
                hello = self._send_recv(sock, HELLO)
            except socket.timeout:
                raise AdapterError(f"{self.ip} 没有回应 miio 握手，可能不是米家设备或不在同一网段")
            did, stamp = struct.unpack(">II", hello[8:16])
            self._id += 1
            payload = json.dumps({"id": self._id, "method": method,
                                  "params": [] if params is None else params}).encode()
            body = self._encrypt(payload)
            header = struct.pack(">HHIII", 0x2131, 32 + len(body), 0, did, stamp + 1)
            checksum = _md5(header + self.token + body)
            try:
                resp = self._send_recv(sock, header + checksum + body)
            except socket.timeout:
                raise AdapterError("设备没有回复，token 可能不对（token 错误时设备会直接忽略请求）")
        finally:
            sock.close()
        if len(resp) <= 32:
            raise AdapterError("设备返回了空响应，token 可能不对")
        try:
            msg = json.loads(self._decrypt(resp[32:]).rstrip(b"\x00").decode())
        except Exception:
            raise AdapterError("无法解密设备响应，token 可能不对")
        if "error" in msg:
            raise AdapterError(f"设备返回错误: {msg['error']}")
        return msg.get("result")


def miio_scan(timeout: float = 2.0) -> list[dict[str, Any]]:
    """向本网段广播 miio 握手包，米家 Wi-Fi 设备会回复自己的设备 id。"""
    from .discovery import local_network

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.settimeout(0.5)
    found: dict[str, dict[str, Any]] = {}
    try:
        for target in (str(local_network().broadcast_address), "255.255.255.255"):
            try:
                sock.sendto(HELLO, (target, MIIO_PORT))
            except OSError:
                pass
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                data, addr = sock.recvfrom(1024)
            except OSError:  # 超时，或 Windows 上的 WinError 10054
                continue
            if len(data) >= 32 and data[:2] == b"\x21\x31":
                did = struct.unpack(">I", data[8:12])[0]
                found[addr[0]] = {"source": "miio", "ip": addr[0], "miio_did": did}
    finally:
        sock.close()
    return list(found.values())


class MiioAdapter(Adapter):
    name = "miio"
    credential_fields = {"token": "米家设备 token，32 位十六进制（可用 Xiaomi Cloud Tokens Extractor 从你的小米账号取出）"}
    actions = {
        "on": "打开（MIoT 设备，默认 siid=2 piid=1），params: {\"siid\": 2, \"piid\": 1} 可选",
        "off": "关闭，参数同 on",
        "get_properties": "读取 MIoT 属性，params: {\"props\": [{\"siid\": 2, \"piid\": 1}]}",
        "set_property": "设置 MIoT 属性，params: {\"siid\": 2, \"piid\": 2, \"value\": 50}",
        "action": "执行 MIoT 动作，params: {\"siid\": 2, \"aiid\": 1, \"in\": []}",
        "call": "老式 miio 设备的原始方法，params: {\"method\": \"set_power\", \"params\": [\"on\"]}",
    }

    def _client(self, device):
        token = device.get("credentials", {}).get("token")
        if not token:
            raise AdapterError("缺少米家设备 token")
        return MiioClient(device["ip"], token)

    async def _call(self, device, method, params=None):
        return await asyncio.to_thread(self._client(device).call, method, params)

    async def pair(self, device, creds):
        info = await self._call({**device, "credentials": creds}, "miIO.info")
        return {**creds, "model": (info or {}).get("model", "")}

    async def get_state(self, device):
        info = await self._call(device, "miIO.info") or {}
        state: dict[str, Any] = {k: info.get(k) for k in ("model", "fw_ver", "mac", "hw_ver") if k in info}
        try:
            state["power"] = await self._call(device, "get_properties",
                                              [{"did": "power", "siid": 2, "piid": 1}])
        except AdapterError as e:
            state["power"] = f"读取失败（可能不是 MIoT 设备）: {e}"
        state["hint"] = "属性编号可在 https://home.miot-spec.com 按 model 查询"
        return state

    async def control(self, device, action, params):
        siid, piid = int(params.get("siid", 2)), int(params.get("piid", 1))
        if action in ("on", "off"):
            r = await self._call(device, "set_properties",
                                 [{"did": "power", "siid": siid, "piid": piid, "value": action == "on"}])
        elif action == "get_properties":
            props = [{"did": f"{p['siid']}-{p['piid']}", "siid": int(p["siid"]), "piid": int(p["piid"])}
                     for p in params.get("props", [])]
            r = await self._call(device, "get_properties", props)
        elif action == "set_property":
            r = await self._call(device, "set_properties",
                                 [{"did": f"{siid}-{piid}", "siid": siid, "piid": piid, "value": params["value"]}])
        elif action == "action":
            r = await self._call(device, "action", {"did": "action", "siid": siid,
                                                    "aiid": int(params["aiid"]), "in": params.get("in", [])})
        elif action == "call":
            r = await self._call(device, params["method"], params.get("params", []))
        else:
            raise AdapterError(f"不支持的动作: {action}")
        return {"result": r}


# ---------------------------------------------------------------------------
# 小米路由器（MiWiFi）本地管理接口
# ---------------------------------------------------------------------------

class XiaomiRouterAdapter(Adapter):
    name = "xiaomi_router"
    credential_fields = {"password": "路由器管理后台密码（登录 192.168.31.1 用的那个）"}
    actions = {
        "sync_devices": "读取连在路由器上的所有设备，并加入设备列表",
    }

    async def _login(self, c: httpx.AsyncClient, ip: str, password: str) -> str:
        page = (await c.get(f"http://{ip}/cgi-bin/luci/web")).text
        key = re.search(r"key:\s*'([0-9a-f]+)'", page)
        mac = re.search(r"var deviceId = '([^']+)'", page)
        if not key:
            raise AdapterError("没在登录页找到加密参数，可能不是小米路由器或固件不支持")
        key, mac = key.group(1), (mac.group(1) if mac else "00:00:00:00:00:00")
        nonce = f"0_{mac}_{int(time.time())}_{random.randint(0, 9999)}"
        new_mode = re.search(r"newEncryptMode\s*[:=]\s*1", page) is not None
        h = hashlib.sha256 if new_mode else hashlib.sha1
        pwd = h((nonce + h((password + key).encode()).hexdigest()).encode()).hexdigest()
        r = await c.post(f"http://{ip}/cgi-bin/luci/api/xqsystem/login",
                         data={"username": "admin", "password": pwd, "logtype": 2, "nonce": nonce})
        data = r.json()
        if data.get("code") != 0 or not data.get("token"):
            raise AdapterError(f"路由器登录失败（密码不对？）: {data.get('msg') or data}")
        return data["token"]

    async def _api(self, device, path: str) -> dict[str, Any]:
        password = device.get("credentials", {}).get("password")
        if not password:
            raise AdapterError("缺少路由器管理密码")
        async with httpx.AsyncClient(timeout=8, trust_env=False) as c:
            stok = await self._login(c, device["ip"], password)
            r = await c.get(f"http://{device['ip']}/cgi-bin/luci/;stok={stok}/api/{path}")
        return r.json()

    async def pair(self, device, creds):
        await self._api({**device, "credentials": creds}, "misystem/status")
        return creds

    async def list_clients(self, device) -> list[dict[str, Any]]:
        data = await self._api(device, "misystem/devicelist")
        out = []
        for d in data.get("list", []):
            ips = d.get("ip") or []
            ip = ips[0].get("ip") if isinstance(ips, list) and ips else (ips if isinstance(ips, str) else "")
            out.append({"ip": ip, "mac": normalize_mac(d.get("mac") or "") if d.get("mac") else "",
                        "name": d.get("name") or d.get("oname") or ip,
                        "online": str(d.get("online", "1")) != "0"})
        return out

    async def get_state(self, device):
        status = await self._api(device, "misystem/status")
        clients = await self.list_clients(device)
        return {"router": {k: status.get(k) for k in ("hardware", "upTime", "cpu", "mem", "count") if k in status},
                "clients": clients}

    async def control(self, device, action, params):
        if action != "sync_devices":
            raise AdapterError(f"不支持的动作: {action}")
        clients = await self.list_clients(device)
        # devices.do_control 看到 discovered 会把它们合并进设备列表
        return {"clients": clients,
                "discovered": [{k: c[k] for k in ("ip", "mac", "name")} for c in clients if c["ip"]]}
