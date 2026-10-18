"""局域网设备发现：mDNS / SSDP / Yeelight 广播 / 米家 miio 握手 / ARP 表。

只扫描本机所在的私有网段，只做标准的发现广播和读取系统 ARP 表，
不做端口爆破、不尝试任何登录。
"""

from __future__ import annotations

import asyncio
import ipaddress
import platform
import re
import socket
import subprocess
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from xml.etree import ElementTree

import httpx

# 常见智能家居相关的 mDNS 服务类型；另外会自动枚举网络里实际出现的类型
COMMON_MDNS_TYPES = [
    "_hap._tcp.local.",            # HomeKit
    "_hue._tcp.local.",            # Philips Hue 桥
    "_home-assistant._tcp.local.",
    "_shelly._tcp.local.",
    "_esphomelib._tcp.local.",
    "_googlecast._tcp.local.",
    "_airplay._tcp.local.",
    "_matter._tcp.local.",
    "_miio._udp.local.",           # 小米 / 米家部分设备
    "_http._tcp.local.",
    "_ipp._tcp.local.",
]


# ---------------------------------------------------------------------------
# 网段
# ---------------------------------------------------------------------------

def local_ip() -> str:
    """通过一次不发包的 UDP connect 拿到出口网卡的 IP。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def local_network(prefix: int = 24) -> ipaddress.IPv4Network:
    return ipaddress.ip_network(f"{local_ip()}/{prefix}", strict=False)


def is_local_private(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return addr.is_private and not addr.is_loopback


# ---------------------------------------------------------------------------
# mDNS
# ---------------------------------------------------------------------------

def mdns_scan(timeout: float = 3.0) -> list[dict[str, Any]]:
    from zeroconf import ServiceBrowser, Zeroconf, ZeroconfServiceTypes

    zc = Zeroconf()
    found: dict[tuple[str, str], dict[str, Any]] = {}
    try:
        try:
            types = set(ZeroconfServiceTypes.find(zc=zc, timeout=min(timeout, 2.0)))
        except Exception:
            types = set()
        types.update(COMMON_MDNS_TYPES)

        class Listener:
            def add_service(self, zc_: Zeroconf, type_: str, name: str) -> None:
                found[(type_, name)] = {"type": type_, "name": name}

            def update_service(self, zc_: Zeroconf, type_: str, name: str) -> None:
                self.add_service(zc_, type_, name)

            def remove_service(self, zc_: Zeroconf, type_: str, name: str) -> None:
                pass

        browsers = [ServiceBrowser(zc, t, Listener()) for t in types]
        time.sleep(timeout)
        for b in browsers:
            b.cancel()

        results = []
        for (type_, name), entry in found.items():
            info = zc.get_service_info(type_, name, timeout=1500)
            if not info:
                continue
            ips = [ip for ip in info.parsed_addresses() if ":" not in ip]
            props = {}
            for k, v in (info.properties or {}).items():
                try:
                    props[k.decode()] = v.decode() if isinstance(v, bytes) else v
                except Exception:
                    continue
            results.append({
                "source": "mdns",
                "ip": ips[0] if ips else None,
                "port": info.port,
                "service_type": type_,
                "name": name.replace("." + type_, ""),
                "hostname": info.server,
                "properties": props,
            })
        return results
    finally:
        zc.close()


# ---------------------------------------------------------------------------
# SSDP / UPnP
# ---------------------------------------------------------------------------

def _ssdp_search(st: str, port: int, timeout: float, extra: str = "") -> list[dict[str, str]]:
    msg = (
        "M-SEARCH * HTTP/1.1\r\n"
        f"HOST: 239.255.255.250:{port}\r\n"
        'MAN: "ssdp:discover"\r\n'
        "MX: 2\r\n"
        f"ST: {st}\r\n{extra}\r\n"
    ).encode()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
    sock.settimeout(0.5)
    responses = []
    try:
        sock.sendto(msg, ("239.255.255.250", port))
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                data, addr = sock.recvfrom(65507)
            except OSError:  # 超时；Windows 上还可能收到 WinError 10054，忽略后继续等
                continue
            headers = {"_ip": addr[0]}
            for line in data.decode(errors="ignore").split("\r\n")[1:]:
                if ":" in line:
                    k, _, v = line.partition(":")
                    headers[k.strip().lower()] = v.strip()
            responses.append(headers)
    finally:
        sock.close()
    return responses


def _upnp_description(location: str) -> dict[str, str]:
    """读取 UPnP 设备描述 XML（只访问局域网地址）。"""
    host = urlparse(location).hostname or ""
    if not is_local_private(host):
        return {}
    try:
        xml = httpx.get(location, timeout=2.0, trust_env=False).text
        root = ElementTree.fromstring(xml)
    except Exception:
        return {}
    out = {}
    for tag in ("friendlyName", "manufacturer", "modelName", "modelNumber", "deviceType"):
        el = root.find(f".//{{*}}{tag}")
        if el is not None and el.text:
            out[tag] = el.text.strip()
    return out


def ssdp_scan(timeout: float = 3.0) -> list[dict[str, Any]]:
    by_usn: dict[str, dict[str, Any]] = {}
    for r in _ssdp_search("ssdp:all", 1900, timeout):
        key = r.get("usn") or r["_ip"]
        by_usn.setdefault(key, {
            "source": "ssdp",
            "ip": r["_ip"],
            "server": r.get("server", ""),
            "st": r.get("st", ""),
            "location": r.get("location", ""),
        })
    # 同一台设备通常会回很多条，按 location 去重后再取描述
    seen_locations: dict[str, dict[str, str]] = {}
    for d in by_usn.values():
        loc = d["location"]
        if loc and loc not in seen_locations:
            seen_locations[loc] = _upnp_description(loc)
        d["description"] = seen_locations.get(loc, {})
    return list(by_usn.values())


def yeelight_scan(timeout: float = 2.0) -> list[dict[str, Any]]:
    """Yeelight 官方局域网协议（需要在 Yeelight App 里打开“局域网控制”）。"""
    results = []
    for r in _ssdp_search("wifi_bulb", 1982, timeout):
        loc = r.get("location", "")  # yeelight://192.168.1.x:55443
        m = re.match(r"yeelight://([\d.]+):(\d+)", loc)
        results.append({
            "source": "yeelight",
            "ip": m.group(1) if m else r["_ip"],
            "port": int(m.group(2)) if m else 55443,
            "model": r.get("model", ""),
            "yeelight_id": r.get("id", ""),
            "name": r.get("name", ""),
            "power": r.get("power", ""),
        })
    return results


# ---------------------------------------------------------------------------
# ARP
# ---------------------------------------------------------------------------

def _wake_arp(network: ipaddress.IPv4Network) -> None:
    """给网段内每个地址发一个空 UDP 包，让系统去做 ARP 解析，从而填满 ARP 表。"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        for host in network.hosts():
            try:
                sock.sendto(b"", (str(host), 9))  # 9 = discard
            except OSError:
                pass
    finally:
        sock.close()
    time.sleep(1.5)


def normalize_mac(mac: str) -> str:
    """统一成 aa:bb:0c:... 形式。macOS 的 arp 会省略前导 0（如 c8:bf:4c:2:21:6a），
    不补齐的话同一台设备在 ARP 和路由器列表里会算成两个 id。"""
    return ":".join(part.zfill(2) for part in mac.replace("-", ":").lower().split(":"))


def read_arp_table() -> list[dict[str, Any]]:
    entries = []
    proc = Path("/proc/net/arp")
    if proc.exists():
        for line in proc.read_text().splitlines()[1:]:
            parts = line.split()
            if len(parts) >= 4 and parts[3] != "00:00:00:00:00:00":
                entries.append({"source": "arp", "ip": parts[0], "mac": normalize_mac(parts[3])})
        return entries
    try:
        out = subprocess.run(["arp", "-a"], capture_output=True, text=True, timeout=5).stdout
    except Exception:
        return entries
    # macOS: ? (192.168.1.5) at aa:bb:cc:dd:ee:ff on en0
    # Windows: 192.168.1.5   aa-bb-cc-dd-ee-ff   dynamic
    for m in re.finditer(r"\(?(\d+\.\d+\.\d+\.\d+)\)?\s+(?:at\s+)?([0-9a-fA-F]{1,2}(?:[:-][0-9a-fA-F]{1,2}){5})", out):
        mac = normalize_mac(m.group(2))
        if mac != "ff:ff:ff:ff:ff:ff":
            entries.append({"source": "arp", "ip": m.group(1), "mac": mac})
    return entries


def arp_scan(sweep: bool) -> list[dict[str, Any]]:
    net = local_network()
    if sweep:
        _wake_arp(net)
    return [e for e in read_arp_table() if ipaddress.ip_address(e["ip"]) in net]


def _reverse_dns(ip: str) -> str:
    try:
        return socket.gethostbyaddr(ip)[0]
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# 合并
# ---------------------------------------------------------------------------

def guess_adapter(entry: dict[str, Any]) -> str:
    """根据发现信息猜测可用的本地控制方式。"""
    text = " ".join(str(v) for v in entry.values()).lower()
    if "yeelight" in entry.get("sources", []) or "yeelight" in text:
        return "yeelight"
    if "miio" in entry.get("sources", []):
        return "miio"
    if "xiaoqiang" in text or "miwifi" in text:
        return "xiaomi_router"
    if "_hue._tcp" in text or "philips hue" in text or "ipbridge" in text:
        return "hue"
    if "shelly" in text:
        return "shelly"
    if "home-assistant" in text or "home assistant" in text:
        return "homeassistant"
    return "unknown"


def discover(sweep: bool = False, timeout: float = 3.0) -> list[dict[str, Any]]:
    """执行所有发现方式并按 IP 合并。"""
    merged: dict[str, dict[str, Any]] = {}

    def add(item: dict[str, Any]) -> None:
        ip = item.get("ip")
        if not ip or not is_local_private(ip):
            return
        d = merged.setdefault(ip, {"ip": ip, "sources": [], "services": []})
        src = item.pop("source")
        if src not in d["sources"]:
            d["sources"].append(src)
        if src == "arp":
            d["mac"] = item["mac"]
        elif src == "mdns":
            d.setdefault("name", item["name"])
            d["services"].append({k: item[k] for k in ("service_type", "port", "name", "properties")})
            d.setdefault("hostname", item["hostname"])
        elif src == "ssdp":
            desc = item.get("description") or {}
            if desc.get("friendlyName"):
                d["name"] = desc["friendlyName"]
            for k in ("manufacturer", "modelName"):
                if desc.get(k):
                    d[k] = desc[k]
            d["services"].append({"ssdp_st": item["st"], "server": item["server"]})
        elif src == "miio":
            d["miio_did"] = item["miio_did"]
        elif src == "yeelight":
            d["name"] = item["name"] or d.get("name") or f"Yeelight {item['model']}"
            d["model"] = item["model"]
            d["port"] = item["port"]

    from .xiaomi import miio_scan

    for fn in (lambda: mdns_scan(timeout), lambda: ssdp_scan(timeout),
               lambda: yeelight_scan(min(timeout, 2.0)), lambda: miio_scan(min(timeout, 2.0)),
               lambda: arp_scan(sweep)):
        try:
            for item in fn():
                add(item)
        except Exception as e:  # 某一种方式失败不影响其它方式
            merged.setdefault("_errors", {"ip": "_errors", "errors": []})["errors"].append(repr(e))

    errors = merged.pop("_errors", None)
    devices = list(merged.values())
    for d in devices:
        d.setdefault("name", _reverse_dns(d["ip"]) or d["ip"])
        d["suggested_adapter"] = guess_adapter(d)
    if errors:
        devices.append({"ip": "", "name": "discovery_errors", "errors": errors["errors"]})
    return devices


async def discover_async(sweep: bool = False, timeout: float = 3.0) -> list[dict[str, Any]]:
    return await asyncio.to_thread(discover, sweep, timeout)


def local_network_info() -> dict[str, Any]:
    return {"local_ip": local_ip(), "network": str(local_network()), "os": platform.system()}
