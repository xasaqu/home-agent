"""桌面版入口：启动后台服务 + 菜单栏/托盘图标 + 自动打开浏览器。

    python -m home_agent.app              # 启动并打开网页
    python -m home_agent.app --background # 开机自启时用，不弹浏览器

打包成 .app / .exe 后双击运行的就是这个文件。
"""

from __future__ import annotations

import logging
import os
import sys
import threading
import time
import webbrowser

# 窗口程序（.app / .exe）没有控制台，stdout/stderr 可能是 None，先兜底，免得打印时崩溃
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w")

import httpx  # noqa: E402

from home_agent import config  # noqa: E402

log = logging.getLogger("home_agent.app")
URL = f"http://127.0.0.1:{config.WEB_PORT}/"


def already_running() -> bool:
    try:
        return httpx.get(URL + "api/status", timeout=1.5, trust_env=False).status_code in (200, 401)
    except httpx.HTTPError:
        return False


def make_icon_image(size: int = 64):
    """画一个蓝底白色小房子，用作菜单栏图标和 App 图标。"""
    from PIL import Image, ImageDraw

    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    s = size
    d.rounded_rectangle([0, 0, s - 1, s - 1], radius=s // 5, fill=(37, 99, 235, 255))
    white = (255, 255, 255, 255)
    d.polygon([(s * 0.5, s * 0.18), (s * 0.15, s * 0.5), (s * 0.85, s * 0.5)], fill=white)  # 屋顶
    d.rectangle([s * 0.25, s * 0.48, s * 0.75, s * 0.82], fill=white)                        # 墙
    d.rectangle([s * 0.44, s * 0.6, s * 0.56, s * 0.82], fill=(37, 99, 235, 255))           # 门
    return img


def start_server():
    import uvicorn

    from home_agent.server import app

    server = uvicorn.Server(uvicorn.Config(app, host=config.WEB_HOST, port=config.WEB_PORT,
                                           log_config=None, access_log=False))
    thread = threading.Thread(target=server.run, name="server", daemon=True)
    thread.start()
    for _ in range(100):  # 最多等 10 秒
        if server.started:
            break
        time.sleep(0.1)
    return server, thread


def main() -> None:
    background = "--background" in sys.argv
    from home_agent.server import setup_logging
    setup_logging(to_file=config.FROZEN)

    if already_running():  # 已经开着了：再次双击图标只打开网页
        if not background:
            webbrowser.open(URL)
        return

    from home_agent import autostart
    if config.FROZEN and config.get("AUTOSTART") == "":
        try:  # 第一次运行默认开启开机自启，可以在菜单或网页设置里关掉
            autostart.set_enabled(True)
        except Exception:
            log.exception("开启开机自启失败")

    server, thread = start_server()
    log.info("Home Agent 已启动：%s（数据目录：%s）", URL, config.DATA_DIR)
    if not background:
        webbrowser.open(URL)

    try:
        import pystray
    except Exception:
        log.warning("没有可用的托盘图标，只运行后台服务")
        thread.join()
        return

    def open_page(icon=None, item=None):
        webbrowser.open(URL)

    def toggle_autostart(icon, item):
        autostart.set_enabled(not autostart.is_enabled())

    def quit_app(icon, item):
        server.should_exit = True
        icon.stop()

    icon = pystray.Icon(
        "HomeAgent", make_icon_image(), "家庭管家",
        menu=pystray.Menu(
            pystray.MenuItem("打开家庭管家", open_page, default=True),
            pystray.MenuItem("开机自动启动", toggle_autostart, checked=lambda item: autostart.is_enabled()),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("退出", quit_app),
        ))
    try:
        icon.run()  # macOS 要求在主线程运行
    except Exception:
        log.exception("托盘图标启动失败，只运行后台服务")
        thread.join()
        return
    thread.join(timeout=5)


if __name__ == "__main__":
    main()
