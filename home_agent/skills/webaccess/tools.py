"""webaccess skill 的工具：web_search / web_fetch。"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import os
import socket
from urllib.parse import urljoin, urlparse

import httpx
from agents import function_tool
from bs4 import BeautifulSoup

UA = "Mozilla/5.0 (compatible; HomeAgent/0.1; +https://github.com/openai/openai-agents-python)"


def _search_sync(query: str, max_results: int) -> list[dict]:
    tavily_key = os.getenv("TAVILY_API_KEY")
    if tavily_key:
        r = httpx.post("https://api.tavily.com/search", timeout=20, json={
            "api_key": tavily_key, "query": query, "max_results": max_results})
        r.raise_for_status()
        return [{"title": x["title"], "url": x["url"], "snippet": x.get("content", "")}
                for x in r.json().get("results", [])]
    from ddgs import DDGS
    backend = os.getenv("WEB_SEARCH_BACKEND", "auto")  # 国内网络可以设成 bing
    results = DDGS().text(query, max_results=max_results, backend=backend)
    return [{"title": x.get("title"), "url": x.get("href"), "snippet": x.get("body")} for x in results]


@function_tool
async def web_search(query: str, max_results: int = 6) -> str:
    """在互联网上搜索，返回标题、链接和摘要。

    Args:
        query: 搜索关键词。
        max_results: 返回条数，1-10。
    """
    try:
        res = await asyncio.to_thread(_search_sync, query, max(1, min(max_results, 10)))
        return json.dumps(res, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"error": f"搜索失败: {e!r}"}, ensure_ascii=False)


_BLOCKED_V4 = [ipaddress.ip_network(n) for n in (
    "0.0.0.0/8", "10.0.0.0/8", "100.64.0.0/10", "127.0.0.0/8", "169.254.0.0/16", "172.16.0.0/12",
    "192.0.0.0/24", "192.168.0.0/16", "224.0.0.0/4", "240.0.0.0/4")]
MAX_REDIRECTS = 5


def _addr_is_public(ip: str) -> bool:
    """局域网、本机、组播等地址一律拦截。注意不能简单用 is_private：Clash、Shadowrocket 等代理的
    fake-ip 网段 (198.18.0.0/15) 也算 private，会误伤所有公网网址，所以这里列出要拦的网段。"""
    addr = ipaddress.ip_address(ip.split("%")[0])
    if addr.version == 6:
        mapped = addr.ipv4_mapped or addr.sixtofour
        if mapped is not None:  # ::ffff:192.168.1.1 这种写法其实是 IPv4 地址
            return _addr_is_public(str(mapped))
        return not (addr.is_private or addr.is_loopback or addr.is_link_local or addr.is_multicast
                    or addr.is_unspecified or addr.is_reserved or addr.is_site_local)
    return not any(addr in n for n in _BLOCKED_V4)


def _resolve(host: str) -> list[str] | None:
    """返回 host 解析出的所有 IP；解析不了返回 None。"""
    try:
        ipaddress.ip_address(host)
        return [host]
    except ValueError:
        pass
    try:
        return sorted({info[4][0] for info in socket.getaddrinfo(host, None)})
    except socket.gaierror:
        return None


def _is_public(host: str) -> bool:
    ips = _resolve(host)
    if ips is None:
        # 本机解析不了（例如只能通过 HTTP 代理上网），交给代理处理，但明显的内网名字直接拒绝
        return host.lower() != "localhost" and not host.lower().endswith((".local", ".lan", ".home", ".localdomain"))
    return all(_addr_is_public(ip) for ip in ips)


class _Blocked(Exception):
    pass


async def _safe_get(client: httpx.AsyncClient, url: str) -> tuple[httpx.Response, str]:
    """GET 一个公网网址。每次跳转都重新检查目标地址；http 请求直接连到检查过的 IP，
    防止“先解析成公网、真正连接时又解析成内网”（DNS 重绑定）。https 由证书校验兜底。"""
    for _ in range(MAX_REDIRECTS + 1):
        parsed = urlparse(url)
        host = parsed.hostname
        if parsed.scheme not in ("http", "https") or not host:
            raise _Blocked("只支持 http/https 网址")
        ips = await asyncio.to_thread(_resolve, host)
        if not await asyncio.to_thread(_is_public, host):
            raise _Blocked("web_fetch 不访问局域网/本机地址，请使用设备工具")
        target, headers = url, {}
        if parsed.scheme == "http" and ips:
            ip = ips[0]
            netloc = f"[{ip}]" if ":" in ip else ip
            if parsed.port:
                netloc += f":{parsed.port}"
            target = parsed._replace(netloc=netloc).geturl()
            headers["Host"] = parsed.netloc.rsplit("@", 1)[-1]
        r = await client.get(target, headers=headers, follow_redirects=False)
        if r.is_redirect and r.headers.get("location"):
            url = urljoin(url, r.headers["location"])
            continue
        return r, url
    raise _Blocked("跳转次数太多")


@function_tool
async def web_fetch(url: str, max_chars: int = 8000) -> str:
    """读取一个公网网页并返回正文文本。

    Args:
        url: 以 http:// 或 https:// 开头的网址。
        max_chars: 最多返回多少字符。
    """
    try:
        async with httpx.AsyncClient(timeout=20, headers={"User-Agent": UA}) as c:
            r, final_url = await _safe_get(c, url)
        ctype = r.headers.get("content-type", "")
        if "html" in ctype:
            soup = BeautifulSoup(r.text, "html.parser")
            for tag in soup(["script", "style", "noscript", "nav", "footer", "header", "svg"]):
                tag.decompose()
            title = soup.title.get_text(strip=True) if soup.title else ""
            text = "\n".join(line for line in (l.strip() for l in soup.get_text("\n").splitlines()) if line)
        else:
            title, text = "", r.text
        return json.dumps({"url": final_url, "status": r.status_code, "title": title,
                           "text": text[: max(500, min(max_chars, 30000))]}, ensure_ascii=False)
    except _Blocked as e:
        return json.dumps({"error": str(e)}, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"error": f"抓取失败: {e!r}"}, ensure_ascii=False)


TOOLS = [web_search, web_fetch]
