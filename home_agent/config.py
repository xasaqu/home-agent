"""集中读取配置。

所有运行时数据（DeepSeek key、设备授权、微信登录、对话记忆）都放在当前用户的应用数据目录：
- macOS：~/Library/Application Support/HomeAgent
- Windows：%APPDATA%\\HomeAgent
- Linux：~/.local/share/HomeAgent
这样装成 App 以后，升级或重装都不会丢配置，重启电脑也不用重新设置。

读取优先级：环境变量 > 数据目录里的 settings.json > 源码目录的 .env（开发时用）> 默认值。
"""

from __future__ import annotations

import json
import os
import secrets
import shutil
import sys
from pathlib import Path
from typing import Any

APP_NAME = "HomeAgent"
FROZEN = getattr(sys, "frozen", False)  # PyInstaller 打包后为 True
ROOT = Path(__file__).resolve().parent.parent


def user_data_dir() -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_NAME
    if sys.platform == "win32":
        return Path(os.getenv("APPDATA") or Path.home() / "AppData" / "Roaming") / APP_NAME
    return Path(os.getenv("XDG_DATA_HOME") or Path.home() / ".local" / "share") / APP_NAME


DATA_DIR = Path(os.getenv("HOME_AGENT_DATA_DIR") or user_data_dir())
DATA_DIR.mkdir(parents=True, exist_ok=True)
SETTINGS_FILE = DATA_DIR / "settings.json"
LOG_DIR = DATA_DIR / "logs"


def _read_dotenv(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        out[key.strip()] = value.strip().strip('"').strip("'")
    return out


def _migrate_legacy() -> None:
    """从源码目录运行过的旧版本：把 data/ 和 .env 里的 key 搬到新的数据目录（只做一次）。"""
    if FROZEN or SETTINGS_FILE.exists():
        return
    legacy = ROOT / "data"
    if legacy.is_dir():
        for f in legacy.iterdir():
            if f.is_file() and not (DATA_DIR / f.name).exists():
                shutil.copy2(f, DATA_DIR / f.name)
    env = _read_dotenv(ROOT / ".env")
    keep = {k: v for k, v in env.items() if v and k in ("DEEPSEEK_API_KEY", "TAVILY_API_KEY")}
    _write_settings(keep)


def _write_settings(data: dict[str, Any]) -> None:
    tmp = SETTINGS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        tmp.chmod(0o600)  # 里面有 API key
    except OSError:
        pass
    tmp.replace(SETTINGS_FILE)


def load_settings() -> dict[str, Any]:
    if SETTINGS_FILE.exists():
        try:
            return json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        except ValueError:
            return {}
    return {}


_migrate_legacy()
_DOTENV = {} if FROZEN else _read_dotenv(ROOT / ".env")
# 其它模块（skill、联网搜索等）直接读环境变量，这里把配置同步进去；已有的环境变量优先
for _k, _v in {**_DOTENV, **load_settings()}.items():
    if isinstance(_v, str) and _v:
        os.environ.setdefault(_k, _v)


def get(key: str, default: str = "") -> str:
    if os.getenv(key):
        return os.environ[key]
    s = load_settings()
    if s.get(key) not in (None, ""):
        return str(s[key])
    return _DOTENV.get(key) or default


def save(**values: Any) -> None:
    """写入 settings.json 并立即生效（不用重启）。"""
    s = load_settings()
    s.update(values)
    _write_settings(s)
    for k, v in values.items():
        if isinstance(v, str):
            os.environ[k] = v
    _refresh()


def _refresh() -> None:
    g = globals()
    g["DEEPSEEK_API_KEY"] = get("DEEPSEEK_API_KEY")
    g["DEEPSEEK_BASE_URL"] = get("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
    g["DEEPSEEK_MODEL"] = get("DEEPSEEK_MODEL", "deepseek-chat")
    g["WECHAT_ENABLED"] = get("WECHAT_ENABLED", "1") == "1"
    g["WECHAT_ALLOW_FROM"] = [x.strip() for x in get("WECHAT_ALLOW_FROM").split(",") if x.strip()]
    g["LAN_ACCESS"] = get("HOME_AGENT_LAN_ACCESS", "0") == "1"


DEEPSEEK_API_KEY = DEEPSEEK_BASE_URL = DEEPSEEK_MODEL = ""
WECHAT_ENABLED = True
LAN_ACCESS = False
WECHAT_ALLOW_FROM: list[str] = []
_refresh()

# 默认只允许本机访问。用户在设置里主动打开“手机访问”后才监听所有网卡，
# 这时除本机以外的访问都必须带 LAN_TOKEN（微信里推送的授权链接会带上它）。
WEB_HOST = get("HOME_AGENT_HOST") or ("0.0.0.0" if LAN_ACCESS else "127.0.0.1")
WEB_PORT = int(get("HOME_AGENT_PORT", "8765"))
# 显式设置后，连本机访问也要口令（老版本行为）
UI_TOKEN = get("HOME_AGENT_UI_TOKEN")
LAN_TOKEN = get("LAN_TOKEN")
if not LAN_TOKEN:
    LAN_TOKEN = secrets.token_urlsafe(24)
    save(LAN_TOKEN=LAN_TOKEN)

DEVICES_FILE = DATA_DIR / "devices.json"
SESSIONS_DB = DATA_DIR / "sessions.sqlite"
WECHAT_FILE = DATA_DIR / "wechat.json"
