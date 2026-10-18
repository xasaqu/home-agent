@echo off
rem 在 Windows 电脑上打包：生成 dist\HomeAgent\HomeAgent.exe，装了 Inno Setup 的话再生成安装程序
rem 需要：Python 3.10+（python.org 安装版，安装时勾选 Add to PATH）
rem 用法：在 packaging 文件夹里双击本文件，或在命令行运行 build_windows.bat
setlocal
chcp 65001 >nul
cd /d "%~dp0"

if not exist ..\.venv (
  python -m venv ..\.venv || goto :error
)
set PY=..\.venv\Scripts\python.exe
%PY% -m pip install -q -r requirements-build.txt || goto :error
%PY% make_icons.py || goto :error
if exist build rmdir /s /q build
if exist dist rmdir /s /q dist
%PY% -m PyInstaller --noconfirm --clean HomeAgent.spec || goto :error

set ISCC="%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
if exist %ISCC% (
  %ISCC% windows_installer.iss || goto :error
  echo 完成：dist\家庭管家-安装程序.exe
) else (
  echo 已生成 dist\HomeAgent\HomeAgent.exe
  echo 想要一个双击安装的 exe，请安装 Inno Setup 6（https://jrsoftware.org/isdl.php）后重新运行本脚本。
)
exit /b 0

:error
echo 打包失败，请查看上面的错误信息。
exit /b 1
