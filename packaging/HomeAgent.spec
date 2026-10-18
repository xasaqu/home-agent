# PyInstaller 打包配置：macOS 生成 HomeAgent.app，Windows 生成 HomeAgent 文件夹（里面有 HomeAgent.exe）。
# 用法见 build_mac.sh / build_windows.bat，不要直接在别的目录运行。
import sys
from pathlib import Path
from PyInstaller.utils.hooks import collect_all, collect_submodules, copy_metadata

ROOT = Path(SPECPATH).parent
ASSETS = Path(SPECPATH) / "build-assets"

datas = [
    (str(ROOT / "home_agent" / "static"), "home_agent/static"),
    (str(ROOT / "home_agent" / "skills"), "home_agent/skills"),   # SKILL.md
]
binaries = []
hiddenimports = collect_submodules("home_agent") + collect_submodules("uvicorn") + collect_submodules("zeroconf")
for pkg in ("ddgs", "agents", "pystray"):
    d, b, h = collect_all(pkg)
    datas += d; binaries += b; hiddenimports += h
# 有些库运行时会查依赖的版本号（importlib.metadata），需要把包信息一起带上
datas += copy_metadata("openai-agents", recursive=True)

a = Analysis(
    [str(ROOT / "home_agent" / "app.py")],
    pathex=[str(ROOT)],
    datas=datas,
    binaries=binaries,
    hiddenimports=hiddenimports,
    excludes=["tkinter"],
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="HomeAgent",
    console=False,   # 不弹黑色命令行窗口
    icon=str(ASSETS / ("icon.icns" if sys.platform == "darwin" else "icon.ico")),
)
coll = COLLECT(exe, a.binaries, a.datas, name="HomeAgent")

if sys.platform == "darwin":
    app = BUNDLE(
        coll,
        name="HomeAgent.app",
        icon=str(ASSETS / "icon.icns"),
        bundle_identifier="com.homeagent.app",
        info_plist={
            "CFBundleDisplayName": "家庭管家",
            "CFBundleName": "家庭管家",
            "CFBundleShortVersionString": "0.2.0",
            "LSUIElement": True,                      # 只在菜单栏显示图标，不占 Dock
            "LSMinimumSystemVersion": "11.0",
            # 扫描局域网设备时，macOS 会用这段话询问“本地网络”权限
            "NSLocalNetworkUsageDescription": "家庭管家需要访问本地网络，才能发现和控制家里的智能设备。",
            "NSBonjourServices": [
                "_hap._tcp", "_hue._tcp", "_home-assistant._tcp", "_shelly._tcp", "_esphomelib._tcp",
                "_googlecast._tcp", "_airplay._tcp", "_matter._tcp", "_miio._udp", "_http._tcp", "_ipp._tcp",
                "_services._dns-sd._udp",
            ],
        },
    )
