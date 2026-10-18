"""生成 App 图标：icon.png（1024）、icon.icns（macOS）、icon.ico（Windows）。"""

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from home_agent.app import make_icon_image  # noqa: E402

out = Path(__file__).resolve().parent / "build-assets"
out.mkdir(exist_ok=True)
big = make_icon_image(1024)
big.save(out / "icon.png")
big.save(out / "icon.ico", sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])

if sys.platform == "darwin":
    iconset = out / "icon.iconset"
    iconset.mkdir(exist_ok=True)
    for size in (16, 32, 128, 256, 512):
        make_icon_image(size).save(iconset / f"icon_{size}x{size}.png")
        make_icon_image(size * 2).save(iconset / f"icon_{size}x{size}@2x.png")
    subprocess.run(["iconutil", "-c", "icns", str(iconset), "-o", str(out / "icon.icns")], check=True)
print("icons ->", out)
