"""开机自动启动：macOS 用 LaunchAgent，Windows 用注册表 Run 项，Linux 用 XDG autostart。

只在当前用户范围内设置，不需要管理员权限。
"""

from __future__ import annotations

import plistlib
import shlex
import sys
from pathlib import Path

from . import config

LABEL = "com.homeagent.app"


def _command() -> list[str]:
    """开机时要执行的命令。--background 表示不自动弹浏览器。"""
    if config.FROZEN:
        exe = Path(sys.executable)
        if sys.platform == "darwin":
            app = next((p for p in exe.parents if p.suffix == ".app"), None)
            if app:
                return ["/usr/bin/open", "-g", "-a", str(app), "--args", "--background"]
        return [str(exe), "--background"]
    return [sys.executable, "-m", "home_agent.app", "--background"]


def _mac_plist() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def _linux_desktop() -> Path:
    return Path.home() / ".config" / "autostart" / "home-agent.desktop"


def is_enabled() -> bool:
    if sys.platform == "darwin":
        return _mac_plist().exists()
    if sys.platform == "win32":
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run") as k:
                winreg.QueryValueEx(k, config.APP_NAME)
            return True
        except OSError:
            return False
    return _linux_desktop().exists()


def set_enabled(enabled: bool) -> bool:
    cmd = _command()
    if sys.platform == "darwin":
        plist = _mac_plist()
        if enabled:
            plist.parent.mkdir(parents=True, exist_ok=True)
            data = {"Label": LABEL, "ProgramArguments": cmd, "RunAtLoad": True}
            if not config.FROZEN:
                data["WorkingDirectory"] = str(config.ROOT)
            plist.write_bytes(plistlib.dumps(data))
        else:
            plist.unlink(missing_ok=True)
    elif sys.platform == "win32":
        import subprocess
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run",
                            0, winreg.KEY_SET_VALUE) as k:
            if enabled:
                winreg.SetValueEx(k, config.APP_NAME, 0, winreg.REG_SZ, subprocess.list2cmdline(cmd))
            else:
                try:
                    winreg.DeleteValue(k, config.APP_NAME)
                except OSError:
                    pass
    else:
        f = _linux_desktop()
        if enabled:
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text("[Desktop Entry]\nType=Application\nName=家庭管家\n"
                         f"Exec={shlex.join(cmd)}\nX-GNOME-Autostart-enabled=true\n", encoding="utf-8")
        else:
            f.unlink(missing_ok=True)
    config.save(AUTOSTART="1" if enabled else "0")
    return is_enabled()
