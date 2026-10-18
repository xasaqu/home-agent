"""本地 Web 服务：HTML 界面 + REST API + 微信收发循环。"""

from __future__ import annotations

import asyncio
import io
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response

from . import agent, config
from .tools import devices
from .tools.adapters import ADAPTERS, AdapterError
from .tools.discovery import discover_async, local_network_info
from .wechat.ilink import WeChatBot

log = logging.getLogger("home_agent")
STATIC = Path(__file__).parent / "static"

HELP = ("可用指令：\n/requests 查看待批准的权限申请\n/approve <编号> 批准\n/deny <编号> 拒绝\n"
        "/reset 清空对话记忆\n有待批准的申请时，也可以直接回复“同意”或“拒绝”（有多条时加上编号，例如“同意 a1b2c3”）。\n其它内容会直接发给管家。")
APPROVE_WORDS = {"同意", "批准", "允许", "好", "好的", "可以", "y", "yes", "ok"}
DENY_WORDS = {"拒绝", "不同意", "不行", "不要", "n", "no"}


def page_url(fragment: str = "") -> str | None:
    """手机可以打开的管家网页地址。只有用户在设置里打开了“手机访问”才有。"""
    if not config.LAN_ACCESS:
        return None
    ip = local_network_info()["local_ip"]
    return f"http://{ip}:{config.WEB_PORT}/?t={config.LAN_TOKEN}" + (f"#{fragment}" if fragment else "")


def _needs_credentials(device_id: str) -> bool:
    d = devices.get(device_id)
    a = ADAPTERS.get(d.get("adapter", ""))
    return bool(a and a.credential_fields and not d.get("credentials"))


def _fill_in_hint() -> str:
    url = page_url("devices")
    return (f"这台设备需要先填写密码或 token，请用连着家里 WiFi 的手机打开：{url}" if url
            else "这台设备需要先填写密码或 token，请在电脑上打开家庭管家，在“设备”页填写。")


def _describe(r: dict[str, Any]) -> str:
    level = "操作" if r["level"] == "control" else "查看"
    return f"[{r['id']}]「{r['device_name']}」的{level}权限：{r['reason']}"


async def _quick_reply(user: str, text: str) -> str | None:
    """“同意 / 拒绝”。只有一条待批准申请时可以不写编号；有多条时必须写编号，
    避免你刚看完一条、又来了一条新的，结果批准错了。"""
    word, _, arg = text.partition(" ")
    if word not in APPROVE_WORDS | DENY_WORDS:
        return None
    reqs = sorted(devices.pending_requests(), key=lambda r: r["ts"])
    if not reqs:
        return None
    if arg.strip():
        r = next((x for x in reqs if x["id"] == arg.strip()), None)
        if not r:
            return f"没有编号为 {arg.strip()} 的待批准申请，发 /requests 查看。"
    elif len(reqs) == 1:
        r = reqs[0]
    else:
        verb = "同意" if word in APPROVE_WORDS else "拒绝"
        return (f"现在有 {len(reqs)} 条申请，请回复“{verb} 编号”：\n"
                + "\n".join(_describe(x) for x in reqs))
    if word in DENY_WORDS:
        devices.deny_request(r["id"])
        return f"已拒绝 {_describe(r)}"
    if _needs_credentials(r["device_id"]):
        return _fill_in_hint()
    try:
        await devices.approve_request(r["id"], by=f"wechat:{user}")
    except Exception as e:
        return f"批准失败：{e}"
    return f"已批准 {_describe(r)}"


async def handle_wechat(user: str, text: str) -> str | None:
    cmd, _, arg = text.strip().partition(" ")
    arg = arg.strip()
    if cmd in ("/help", "/帮助"):
        return HELP
    if cmd == "/requests":
        reqs = devices.pending_requests()
        return "\n".join(_describe(r) for r in reqs) or "没有待批准的申请。"
    if cmd == "/approve" and arg:
        try:
            req = next((r for r in devices.pending_requests() if r["id"] == arg), None)
            if req and _needs_credentials(req["device_id"]):
                return _fill_in_hint()
            d = await devices.approve_request(arg, by=f"wechat:{user}")
            return f"已批准：{d['name']} → {d['permission']}"
        except Exception as e:
            return f"批准失败：{e}"
    if cmd == "/deny" and arg:
        devices.deny_request(arg)
        return "已拒绝。"
    if cmd == "/reset":
        await agent.reset(f"wechat:{user}")
        return "对话记忆已清空。"
    quick = await _quick_reply(user, " ".join(text.strip().lower().rstrip("。!！").split()))
    if quick:
        return quick
    return await agent.ask(text, session_id=f"wechat:{user}", source="wechat")


bot = WeChatBot(handle_wechat)
_loop: asyncio.AbstractEventLoop | None = None


def _on_request(req: dict[str, Any]) -> None:
    level = "操作" if req["level"] == "control" else "查看"
    text = f"管家想获得「{req['device_name']}」的{level}权限：{req['reason']}\n"
    if _needs_credentials(req["device_id"]):
        text += _fill_in_hint()
    else:
        text += f"回复“同意”批准，回复“拒绝”拒绝（编号 {req['id']}）。"
        url = page_url("devices")
        if url:
            text += f"\n也可以在手机上打开：{url}"
    # 工具可能在线程里执行，用 threadsafe 方式投递到主事件循环
    if _loop is not None:
        asyncio.run_coroutine_threadsafe(bot.notify_owner(text), _loop)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _loop
    _loop = asyncio.get_running_loop()
    devices.request_listeners.append(_on_request)
    if config.WECHAT_ENABLED:
        bot.start()
    yield
    bot.stop()


app = FastAPI(title="Home Agent", lifespan=lifespan)


def _allowed_hosts() -> set[str]:
    hosts = {f"127.0.0.1:{config.WEB_PORT}", f"localhost:{config.WEB_PORT}", f"[::1]:{config.WEB_PORT}"}
    if config.WEB_HOST != "127.0.0.1":
        hosts.add(f"{local_network_info()['local_ip']}:{config.WEB_PORT}")
    return hosts


@app.middleware("http")
async def check_origin(request: Request, call_next):
    """挡住浏览器里其它网站对管家接口的调用：
    - Host 必须是本机地址（防 DNS 重绑定：恶意域名解析到 127.0.0.1 时 Host 是那个域名）；
    - 会改东西的请求必须来自管家自己的页面（Origin 一致，并带上自定义请求头，
      跨站页面带这个头会触发浏览器的 CORS 预检，而管家不允许跨站，所以发不出来）。"""
    allowed = _allowed_hosts()
    if request.headers.get("host", "") not in allowed:
        return JSONResponse({"detail": "不允许的访问地址"}, status_code=403)
    if request.url.path.startswith("/api/") and request.method not in ("GET", "HEAD", "OPTIONS"):
        origin = request.headers.get("origin")
        if origin and origin.split("://", 1)[-1] not in allowed:
            return JSONResponse({"detail": "不允许其它网站调用管家"}, status_code=403)
        if request.headers.get("x-home-agent") != "1":
            return JSONResponse({"detail": "缺少请求头"}, status_code=403)
    return await call_next(request)


@app.middleware("http")
async def check_token(request: Request, call_next):
    if request.url.path.startswith("/api/"):
        token = request.headers.get("x-token") or request.query_params.get("token")
        is_local = (request.client.host if request.client else "") in ("127.0.0.1", "::1")
        if config.UI_TOKEN:
            ok = token == config.UI_TOKEN
        else:  # 本机直接放行；手机等其它设备必须带 LAN_TOKEN
            ok = is_local or (bool(config.LAN_TOKEN) and token == config.LAN_TOKEN)
        if not ok:
            return JSONResponse({"detail": "需要口令"}, status_code=401)
    return await call_next(request)


@app.exception_handler(AdapterError)
async def adapter_error(_, e: AdapterError):
    return JSONResponse({"detail": str(e)}, status_code=400)


@app.exception_handler(httpx.HTTPError)
async def lan_http_error(_, e: httpx.HTTPError):
    msg = f"连不上设备：{e or type(e).__name__}"
    if "No route to host" in str(e) or isinstance(e, httpx.ConnectError):
        msg += "。如果是 macOS，请在“系统设置 → 隐私与安全性 → 本地网络”里允许运行本程序的应用（终端等）访问本地网络"
    return JSONResponse({"detail": msg}, status_code=502)


@app.exception_handler(PermissionError)
async def permission_error(_, e: PermissionError):
    return JSONResponse({"detail": str(e)}, status_code=403)


@app.exception_handler(KeyError)
async def key_error(_, e: KeyError):
    return JSONResponse({"detail": str(e).strip("'\"")}, status_code=404)


@app.get("/")
async def index():
    return FileResponse(STATIC / "index.html")


@app.get("/api/status")
async def status():
    return {"model": config.DEEPSEEK_MODEL, "base_url": config.DEEPSEEK_BASE_URL,
            "api_key_configured": bool(config.DEEPSEEK_API_KEY),
            "network": local_network_info(), "ui_token_required": bool(config.UI_TOKEN),
            "setup_done": config.get("SETUP_DONE") == "1"}


# ---------------- 设置 / 首次启动向导 ----------------

@app.get("/api/settings")
async def get_settings():
    from . import autostart
    key = config.DEEPSEEK_API_KEY
    return {"api_key_configured": bool(key), "api_key_hint": f"{key[:3]}…{key[-4:]}" if len(key) > 8 else "",
            "autostart": autostart.is_enabled(), "lan_access": config.LAN_ACCESS,
            "lan_access_active": config.WEB_HOST != "127.0.0.1", "phone_url": page_url(),
            "wechat": bot.info(), "data_dir": str(config.DATA_DIR),
            "setup_done": config.get("SETUP_DONE") == "1"}


@app.post("/api/settings/key")
async def set_key(key: str = Body(..., embed=True)):
    key = key.strip()
    if not key.startswith("sk-") or len(key) < 20:
        raise HTTPException(400, "格式不对：DeepSeek 的 key 以 sk- 开头")
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.get(f"{config.DEEPSEEK_BASE_URL}/models", headers={"Authorization": f"Bearer {key}"})
    except httpx.HTTPError as e:
        raise HTTPException(502, f"连不上 DeepSeek，请检查网络：{e!r}")
    if r.status_code == 401:
        raise HTTPException(400, "DeepSeek 说这个 key 无效，请重新复制一次")
    if r.status_code >= 400:
        raise HTTPException(400, f"DeepSeek 返回错误 HTTP {r.status_code}")
    config.save(DEEPSEEK_API_KEY=key)
    agent.invalidate()
    return await get_settings()


@app.post("/api/settings/autostart")
async def set_autostart(enabled: bool = Body(..., embed=True)):
    from . import autostart
    autostart.set_enabled(enabled)
    return await get_settings()


@app.post("/api/settings/lan")
async def set_lan(enabled: bool = Body(..., embed=True)):
    """打开或关闭“手机访问”。改监听地址需要重启程序才生效。"""
    config.save(HOME_AGENT_LAN_ACCESS="1" if enabled else "0")
    return await get_settings()


@app.post("/api/settings/done")
async def setup_done():
    config.save(SETUP_DONE="1")
    return await get_settings()


# ---------------- 对话 ----------------

@app.post("/api/chat")
async def chat(message: str = Body(...), session_id: str = Body("web")):
    if not config.DEEPSEEK_API_KEY:
        raise HTTPException(400, "没有设置 DEEPSEEK_API_KEY")
    try:
        reply = await agent.ask(message, session_id=f"web:{session_id}", source="web")
    except Exception as e:
        log.exception("agent failed")
        raise HTTPException(500, f"Agent 出错：{e}")
    return {"reply": reply}


@app.post("/api/chat/reset")
async def chat_reset(session_id: str = Body("web", embed=True)):
    await agent.reset(f"web:{session_id}")
    return {"ok": True}


# ---------------- 设备 ----------------

@app.get("/api/devices")
async def list_devices():
    return {"devices": devices.list_all(), "requests": devices.pending_requests(),
            "adapters": {k: {"actions": a.actions, "credential_fields": a.credential_fields}
                         for k, a in ADAPTERS.items()},
            "log": devices.recent_log(30)}


@app.post("/api/discover")
async def discover(sweep: bool = Body(False, embed=True)):
    found = await discover_async(sweep, 3.0)
    devices.merge_discovered(found)
    return await list_devices()


@app.post("/api/devices/{device_id}/grant")
async def grant(device_id: str, level: str = Body(...), adapter: str | None = Body(None),
                credentials: dict[str, str] | None = Body(None)):
    return await devices.grant(device_id, level, adapter, credentials, by="web")


@app.patch("/api/devices/{device_id}")
async def rename(device_id: str, alias: str = Body(..., embed=True)):
    return devices.update(device_id, alias=alias, name=alias)


@app.delete("/api/devices/{device_id}")
async def delete(device_id: str):
    devices.remove(device_id)
    return {"ok": True}


@app.get("/api/devices/{device_id}/state")
async def state(device_id: str):
    return await devices.read_state(device_id)


@app.post("/api/devices/{device_id}/control")
async def control(device_id: str, action: str = Body(...), params: dict[str, Any] = Body({})):
    return await devices.do_control(device_id, action, params, by="web")


@app.post("/api/requests/{req_id}/approve")
async def approve(req_id: str):
    return await devices.approve_request(req_id, by="web")


@app.post("/api/requests/{req_id}/deny")
async def deny(req_id: str):
    devices.deny_request(req_id)
    return {"ok": True}


# ---------------- 微信 ----------------

@app.get("/api/wechat")
async def wechat_info():
    return bot.info()


@app.post("/api/wechat/login")
async def wechat_login():
    try:
        return await bot.start_login()
    except Exception as e:
        raise HTTPException(502, f"获取二维码失败：{e}")


@app.post("/api/wechat/verify")
async def wechat_verify(code: str = Body(..., embed=True)):
    bot.submit_verify_code(code)
    return bot.info()


@app.post("/api/wechat/logout")
async def wechat_logout():
    bot.logout()
    return bot.info()


# ---------------- 米家：扫码读取设备 token ----------------

from .tools.micloud import MiCloudLogin  # noqa: E402

micloud = MiCloudLogin()


@app.post("/api/micloud/login")
async def micloud_login():
    try:
        return await micloud.start(devices.apply_miio_tokens)
    except Exception as e:
        raise HTTPException(502, f"获取二维码失败：{e}")


@app.get("/api/micloud")
async def micloud_info():
    return micloud.info()


@app.get("/api/qr")
async def qr(data: str):
    """把字符串渲染成二维码 SVG（用于微信扫码登录）。"""
    import qrcode
    import qrcode.image.svg

    buf = io.BytesIO()
    qrcode.make(data, image_factory=qrcode.image.svg.SvgPathImage, box_size=8).save(buf)
    return Response(buf.getvalue(), media_type="image/svg+xml")


def setup_logging(to_file: bool = False) -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler()] if not to_file else []
    if to_file:
        from logging.handlers import RotatingFileHandler
        config.LOG_DIR.mkdir(parents=True, exist_ok=True)
        handlers.append(RotatingFileHandler(config.LOG_DIR / "home-agent.log", maxBytes=2_000_000,
                                            backupCount=3, encoding="utf-8"))
    logging.basicConfig(level=logging.INFO, handlers=handlers,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # 微信长轮询会刷屏


def main() -> None:
    """命令行方式运行（开发用）。普通用户用 home_agent.app 启动，带菜单栏图标。"""
    import uvicorn

    setup_logging()
    print(f"Home Agent 已启动：http://127.0.0.1:{config.WEB_PORT}（数据目录：{config.DATA_DIR}）")
    uvicorn.run(app, host=config.WEB_HOST, port=config.WEB_PORT)
