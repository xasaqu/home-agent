"""微信 ClawBot（iLink Bot 协议）的 Python 客户端。

协议参照腾讯官方开源的 OpenClaw 微信插件 @tencent-weixin/openclaw-weixin（MIT）：
- 扫码登录：POST ilink/bot/get_bot_qrcode → GET ilink/bot/get_qrcode_status 长轮询
- 收消息：POST ilink/bot/getupdates 长轮询（带 get_updates_buf 游标）
- 发消息：POST ilink/bot/sendmessage（回复时带上收到消息里的 context_token）
- 输入中：POST ilink/bot/getconfig 取 typing_ticket → POST ilink/bot/sendtyping

ClawBot 目前仍在灰度，需要手机微信里“我 → 设置 → 插件”能看到 ClawBot。
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import re
import secrets
import time
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from .. import config

log = logging.getLogger("home_agent.wechat")

# 微信服务器在国内，直连；不走系统代理（代理软件常常会对长轮询返回 503）
FIXED_BASE_URL = os.getenv("WECHAT_ILINK_BASE_URL", "https://ilinkai.weixin.qq.com")
ILINK_APP_ID = "bot"
BOT_TYPE = "3"
# 与上游插件版本保持一致（uint32: major<<16 | minor<<8 | patch），服务端可能按版本下发能力
UPSTREAM_PLUGIN_VERSION = "2.4.10"
BOT_AGENT = "HomeAgent/0.1"
STALE_TOKEN_ERRCODE = -14
TEXT_CHUNK = 3500

MSG_TYPE_USER = 1
MSG_TYPE_BOT = 2
ITEM_TEXT = 1
ITEM_VOICE = 3
STATE_FINISH = 2


def _client_version(v: str) -> int:
    major, minor, patch = (int(x) for x in (v.split(".") + ["0", "0"])[:3])
    return ((major & 0xFF) << 16) | ((minor & 0xFF) << 8) | (patch & 0xFF)


def _common_headers() -> dict[str, str]:
    return {"iLink-App-Id": ILINK_APP_ID,
            "iLink-App-ClientVersion": str(_client_version(UPSTREAM_PLUGIN_VERSION))}


def _post_headers(token: str | None) -> dict[str, str]:
    uin = base64.b64encode(str(secrets.randbits(32)).encode()).decode()
    h = {"Content-Type": "application/json", "AuthorizationType": "ilink_bot_token",
         "X-WECHAT-UIN": uin, **_common_headers()}
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


def _base_info() -> dict[str, str]:
    return {"channel_version": UPSTREAM_PLUGIN_VERSION, "bot_agent": BOT_AGENT}


def _parse(raw: str) -> dict[str, Any]:
    # message_id 等是 uint64，Python int 本身不丢精度，直接解析即可
    return json.loads(raw) if raw.strip() else {}


def strip_markdown(text: str) -> str:
    """微信聊天不渲染 Markdown，做一点简单清理。"""
    text = re.sub(r"```[a-zA-Z]*\n?", "", text)
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    text = re.sub(r"^#{1,6}\s*", "", text, flags=re.M)
    text = re.sub(r"\[([^\]]+)\]\((https?://[^)]+)\)", r"\1 \2", text)
    return text.strip()


class WeChatBot:
    """一个微信账号的收发循环。消息处理交给 on_message 回调。"""

    def __init__(self, on_message: Callable[[str, str], Awaitable[str | None]]):
        self.on_message = on_message
        self.state: dict[str, Any] = self._load()
        self.login: dict[str, Any] = {}       # 进行中的扫码登录
        self.status = "logged_in" if self.state.get("token") else "logged_out"
        self.last_error = ""
        self._task: asyncio.Task | None = None
        self._login_task: asyncio.Task | None = None

    # ---------------- 持久化 ----------------

    def _load(self) -> dict[str, Any]:
        if config.WECHAT_FILE.exists():
            return json.loads(config.WECHAT_FILE.read_text(encoding="utf-8"))
        return {}

    def _save(self) -> None:
        config.WECHAT_FILE.write_text(json.dumps(self.state, ensure_ascii=False, indent=2), encoding="utf-8")
        try:
            config.WECHAT_FILE.chmod(0o600)  # 里面有 bot_token
        except OSError:
            pass

    @property
    def base_url(self) -> str:
        return (self.state.get("base_url") or FIXED_BASE_URL).rstrip("/")

    def allowed(self, user_id: str) -> bool:
        allow = set(config.WECHAT_ALLOW_FROM)
        if self.state.get("owner_user_id"):
            allow.add(self.state["owner_user_id"])
        return user_id in allow

    def info(self) -> dict[str, Any]:
        return {"status": self.status, "bot_id": self.state.get("bot_id"),
                "owner_user_id": self.state.get("owner_user_id"), "last_error": self.last_error,
                "login": {k: v for k, v in self.login.items() if k in ("qrcode_url", "status", "message")}}

    # ---------------- HTTP ----------------

    async def _post(self, endpoint: str, body: dict[str, Any], timeout: float = 15,
                    base_url: str | None = None, token: str | None = "self") -> dict[str, Any]:
        tok = self.state.get("token") if token == "self" else token
        async with httpx.AsyncClient(trust_env=False, timeout=timeout) as c:
            r = await c.post(f"{base_url or self.base_url}/{endpoint}",
                             content=json.dumps({**body, "base_info": _base_info()}),
                             headers=_post_headers(tok))
        r.raise_for_status()
        return _parse(r.text)

    # ---------------- 扫码登录 ----------------

    async def start_login(self) -> dict[str, Any]:
        tokens = [self.state["token"]] if self.state.get("token") else []
        async with httpx.AsyncClient(trust_env=False, timeout=15) as c:
            r = await c.post(f"{FIXED_BASE_URL}/ilink/bot/get_bot_qrcode", params={"bot_type": BOT_TYPE},
                             content=json.dumps({"local_token_list": tokens}), headers=_post_headers(None))
        r.raise_for_status()
        data = _parse(r.text)
        self.login = {"qrcode": data["qrcode"], "qrcode_url": data["qrcode_img_content"],
                      "status": "wait", "message": "请用手机微信扫码", "base": FIXED_BASE_URL,
                      "verify_code": None, "started": time.time()}
        if self._login_task and not self._login_task.done():
            self._login_task.cancel()
        self._login_task = asyncio.create_task(self._poll_login())
        return self.info()

    def submit_verify_code(self, code: str) -> None:
        self.login["verify_code"] = code.strip()

    async def _poll_login(self) -> None:
        lg = self.login
        deadline = time.time() + 480
        while time.time() < deadline:
            params = {"qrcode": lg["qrcode"]}
            if lg.get("verify_code"):
                params["verify_code"] = lg["verify_code"]
            try:
                async with httpx.AsyncClient(trust_env=False, timeout=40) as c:
                    r = await c.get(f"{lg['base']}/ilink/bot/get_qrcode_status", params=params,
                                    headers=_common_headers())
                s = _parse(r.text) if r.status_code == 200 else {"status": "wait"}
            except (httpx.TimeoutException, httpx.TransportError):
                s = {"status": "wait"}
            status = s.get("status", "wait")
            lg["status"] = status
            if status == "scaned":
                lg["verify_code"] = None
                lg["message"] = "已扫码，请在手机上确认"
            elif status == "need_verifycode":
                lg["message"] = "请输入手机微信上显示的数字" if not lg.get("verify_code") else "数字不匹配，请重新输入"
                lg["verify_code"] = None
                while not lg.get("verify_code") and time.time() < deadline:
                    await asyncio.sleep(0.5)
                continue
            elif status == "scaned_but_redirect" and s.get("redirect_host"):
                lg["base"] = f"https://{s['redirect_host']}"
            elif status in ("expired", "verify_code_blocked"):
                lg["message"] = "二维码已失效，请重新获取"
                return
            elif status == "binded_redirect":
                lg["message"] = "这个微信已经绑定过，沿用现有凭证"
                self.status = "logged_in" if self.state.get("token") else "logged_out"
                return
            elif status == "confirmed":
                self.state.update(token=s.get("bot_token"), bot_id=s.get("ilink_bot_id"),
                                  base_url=s.get("baseurl") or FIXED_BASE_URL,
                                  owner_user_id=s.get("ilink_user_id"), get_updates_buf="")
                self._save()
                lg["message"] = "绑定成功"
                self.status = "logged_in"
                self.start()
                return
            await asyncio.sleep(1)
        lg["status"], lg["message"] = "expired", "登录超时，请重新获取二维码"

    def logout(self) -> None:
        self.stop()
        self.state = {}
        self._save()
        self.status = "logged_out"

    # ---------------- 收发 ----------------

    def start(self) -> None:
        if self.state.get("token") and (self._task is None or self._task.done()):
            self._task = asyncio.create_task(self._loop())

    def stop(self) -> None:
        if self._task:
            self._task.cancel()

    async def _loop(self) -> None:
        log.info("wechat loop started, bot=%s", self.state.get("bot_id"))
        try:
            await self._post("ilink/bot/msg/notifystart", {}, timeout=10)
        except Exception as e:
            log.debug("notifystart failed: %r", e)
        poll_timeout = 35.0
        failures = 0
        while True:
            try:
                resp = await self._post("ilink/bot/getupdates",
                                        {"get_updates_buf": self.state.get("get_updates_buf", "")},
                                        timeout=poll_timeout + 5)
            except httpx.TimeoutException:
                continue
            except asyncio.CancelledError:
                raise
            except Exception as e:
                failures += 1
                self.last_error = repr(e)
                log.warning("getupdates error: %r", e)
                await asyncio.sleep(min(30, 2 ** failures))
                continue

            if resp.get("longpolling_timeout_ms"):
                poll_timeout = resp["longpolling_timeout_ms"] / 1000
            err = resp.get("errcode") or resp.get("ret") or 0
            if err:
                if err == STALE_TOKEN_ERRCODE:
                    self.status, self.last_error = "expired", "微信登录已过期，请重新扫码"
                    log.warning(self.last_error)
                    return
                failures += 1
                self.last_error = f"getupdates ret={resp.get('ret')} errcode={resp.get('errcode')} {resp.get('errmsg', '')}"
                await asyncio.sleep(min(30, 2 ** failures))
                continue
            failures = 0
            if resp.get("get_updates_buf"):
                self.state["get_updates_buf"] = resp["get_updates_buf"]
                self._save()
            for msg in resp.get("msgs") or []:
                asyncio.create_task(self._handle(msg))

    @staticmethod
    def _text_of(msg: dict[str, Any]) -> str:
        parts = []
        for item in msg.get("item_list") or []:
            if item.get("type") == ITEM_TEXT and (item.get("text_item") or {}).get("text") is not None:
                parts.append(str(item["text_item"]["text"]))
            elif item.get("type") == ITEM_VOICE and (item.get("voice_item") or {}).get("text"):
                parts.append(item["voice_item"]["text"])
        return "\n".join(parts).strip()

    async def _handle(self, msg: dict[str, Any]) -> None:
        if msg.get("message_type") != MSG_TYPE_USER:
            return
        user = msg.get("from_user_id", "")
        ctx_token = msg.get("context_token")
        if not self.allowed(user):
            log.warning("ignored message from non-allowed user %s", user)
            return
        if ctx_token:
            self.state.setdefault("context_tokens", {})[user] = ctx_token
            self._save()
        text = self._text_of(msg)
        if not text:
            await self.send_text(user, "目前只支持文字和语音（转文字）消息。", ctx_token)
            return
        stop_typing = await self._typing(user, ctx_token)
        try:
            reply = await self.on_message(user, text)
        except Exception as e:
            log.exception("agent failed")
            reply = f"处理出错了：{e}"
        finally:
            stop_typing()
        if reply:
            await self.send_text(user, reply, ctx_token)

    async def _typing(self, user: str, ctx_token: str | None) -> Callable[[], None]:
        """显示“对方正在输入”，返回停止函数。失败时静默。"""
        try:
            cfg = await self._post("ilink/bot/getconfig",
                                   {"ilink_user_id": user, "context_token": ctx_token}, timeout=10)
            ticket = cfg.get("typing_ticket")
        except Exception:
            ticket = None
        if not ticket:
            return lambda: None

        async def keep() -> None:
            try:
                while True:
                    await self._post("ilink/bot/sendtyping",
                                     {"ilink_user_id": user, "typing_ticket": ticket, "status": 1}, timeout=10)
                    await asyncio.sleep(5)
            except asyncio.CancelledError:
                try:
                    await self._post("ilink/bot/sendtyping",
                                     {"ilink_user_id": user, "typing_ticket": ticket, "status": 2}, timeout=10)
                except Exception:
                    pass
            except Exception:
                pass

        task = asyncio.create_task(keep())
        return task.cancel

    async def send_text(self, user: str, text: str, ctx_token: str | None = None) -> None:
        ctx_token = ctx_token or self.state.get("context_tokens", {}).get(user)
        text = strip_markdown(text)
        for i in range(0, len(text), TEXT_CHUNK):
            body = {"msg": {
                "from_user_id": "", "to_user_id": user,
                "client_id": f"home-agent:{int(time.time() * 1000)}-{secrets.token_hex(4)}",
                "message_type": MSG_TYPE_BOT, "message_state": STATE_FINISH,
                "item_list": [{"type": ITEM_TEXT, "text_item": {"text": text[i:i + TEXT_CHUNK]}}],
                **({"context_token": ctx_token} if ctx_token else {}),
            }}
            resp = await self._post("ilink/bot/sendmessage", body)
            if resp.get("ret"):
                raise RuntimeError(f"sendmessage ret={resp['ret']} {resp.get('errmsg', '')}")

    async def notify_owner(self, text: str) -> None:
        """主动给绑定的主人发消息（例如有新的权限申请）。需要对方先发过消息才有 context_token。"""
        owner = self.state.get("owner_user_id")
        if owner and self.status == "logged_in" and self.state.get("context_tokens", {}).get(owner):
            try:
                await self.send_text(owner, text)
            except Exception as e:
                log.warning("notify owner failed: %r", e)
