#!/bin/bash
# 在 Mac 上打包：生成 dist/HomeAgent.app 和 dist/家庭管家.dmg
# 用法：cd packaging && ./build_mac.sh
set -euo pipefail
cd "$(dirname "$0")"
PY=${PYTHON:-../.venv/bin/python}

"$PY" -m pip install -q -r requirements-build.txt
"$PY" make_icons.py
rm -rf build dist
"$PY" -m PyInstaller --noconfirm --clean HomeAgent.spec

# 没有 Apple 开发者证书时做一次 ad-hoc 签名，Apple Silicon 上才能运行
codesign --force --deep --sign - dist/HomeAgent.app

# 做 dmg：里面放 App 和一个“应用程序”快捷方式，拖进去就装好了
STAGE=build/dmg
rm -rf "$STAGE" && mkdir -p "$STAGE"
cp -R dist/HomeAgent.app "$STAGE/家庭管家.app"
ln -s /Applications "$STAGE/应用程序"
hdiutil create -volname "家庭管家" -srcfolder "$STAGE" -ov -format UDZO "dist/家庭管家.dmg"
echo "完成：$(pwd)/dist/家庭管家.dmg"
